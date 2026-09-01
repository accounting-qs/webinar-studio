"""Mailbox-provider resolution for contact email domains.

Why MX and not the domain name: the invited audience is ~98% company domains
(a 253k-contact webinar had gmail+hotmail+yahoo at 1.2% combined, and its top
25 domains covered 2.3%). "acme.com" tells you nothing about whether the
Google Calendar invite lands in Gmail or Outlook — the MX record does.

Resolution is one DNS round trip per domain, but the answer is stable, so
every domain is resolved once into `email_domain_provider` and reused by every
later webinar. `resolve_domains()` is resumable: it only looks up domains the
cache does not already hold.

Security gateways (Proofpoint, Mimecast, Barracuda, …) front the real mailbox
and hide it, so they get their own labels rather than being folded into
"Other" — counting them as a provider would be wrong, and counting them as
Google or Microsoft would be a guess.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# Public resolvers, used round-robin so no single one absorbs the whole run.
_RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "8.8.4.4"]
_TIMEOUT = 4.0
_ATTEMPTS = 2

# Consumer mailboxes, matched on the domain itself — their MX is the same
# infrastructure as the business product, so MX alone can't separate
# "someone@gmail.com" from a Google Workspace seat.
_CONSUMER_DOMAINS: dict[str, str] = {
    "gmail.com": "Gmail (consumer)",
    "googlemail.com": "Gmail (consumer)",
    "outlook.com": "Outlook (consumer)",
    "hotmail.com": "Outlook (consumer)",
    "hotmail.co.uk": "Outlook (consumer)",
    "hotmail.fr": "Outlook (consumer)",
    "live.com": "Outlook (consumer)",
    "live.co.uk": "Outlook (consumer)",
    "msn.com": "Outlook (consumer)",
    "yahoo.com": "Yahoo",
    "yahoo.co.uk": "Yahoo",
    "yahoo.ca": "Yahoo",
    "yahoo.fr": "Yahoo",
    "yahoo.de": "Yahoo",
    "ymail.com": "Yahoo",
    "rocketmail.com": "Yahoo",
    "aol.com": "AOL",
    "icloud.com": "Apple iCloud",
    "me.com": "Apple iCloud",
    "mac.com": "Apple iCloud",
    "proton.me": "Proton",
    "protonmail.com": "Proton",
    "pm.me": "Proton",
    "gmx.de": "GMX / web.de",
    "gmx.net": "GMX / web.de",
    "web.de": "GMX / web.de",
    "t-online.de": "T-Online",
}

# MX-host substring -> provider. Ordered: the first match wins, so the
# gateways sit above the platforms they front.
_MX_RULES: tuple[tuple[str, str], ...] = (
    ("pphosted.com", "Proofpoint (gateway)"),
    ("ppe-hosted.com", "Proofpoint (gateway)"),
    ("mimecast.com", "Mimecast (gateway)"),
    ("barracudanetworks.com", "Barracuda (gateway)"),
    ("messagelabs.com", "Symantec (gateway)"),
    ("trendmicro.com", "Trend Micro (gateway)"),
    ("cisco.com", "Cisco IronPort (gateway)"),
    ("iphmx.com", "Cisco IronPort (gateway)"),
    ("sophos.com", "Sophos (gateway)"),
    ("fortimail", "Fortinet (gateway)"),
    ("protection.outlook.com", "Microsoft 365"),
    ("outlook.com", "Microsoft 365"),
    ("hotmail.com", "Microsoft 365"),
    ("google.com", "Google Workspace"),
    ("googlemail.com", "Google Workspace"),
    ("zoho.com", "Zoho"),
    ("zoho.eu", "Zoho"),
    ("protonmail.ch", "Proton"),
    ("proton.me", "Proton"),
    ("yahoodns.net", "Yahoo"),
    ("icloud.com", "Apple iCloud"),
    ("apple.com", "Apple iCloud"),
    ("secureserver.net", "GoDaddy"),
    ("ionos.", "IONOS"),
    ("1and1.", "IONOS"),
    ("kundenserver.de", "IONOS"),
    ("ovh.net", "OVH"),
    ("yandex", "Yandex"),
    ("mail.ru", "Mail.ru"),
    ("qq.com", "Tencent QQ"),
    ("163.com", "NetEase"),
    ("aliyun.com", "Alibaba"),
    ("rackspace.com", "Rackspace"),
    ("emailsrvr.com", "Rackspace"),
    ("titan.email", "Titan"),
    ("hostinger", "Hostinger"),
    ("bluehost.com", "Bluehost"),
    ("hostgator.com", "HostGator"),
    ("namecheap", "Namecheap"),
    ("privateemail.com", "Namecheap"),
    ("register.com", "Register.com"),
    ("mailgun", "Mailgun"),
    ("sendgrid", "SendGrid"),
    ("amazonaws.com", "Amazon SES/WorkMail"),
    ("awsapps.com", "Amazon SES/WorkMail"),
)

# Statuses that are worth retrying on a later run (transient), vs settled.
RETRYABLE_STATUSES = ("timeout", "error")


# Labels for domains that resolved to no usable mailbox. Kept apart because
# they mean different things operationally: a domain that does not exist is bad
# list data, while one that exists without MX is a real company that simply
# cannot receive mail at that domain.
_STATUS_LABELS = {
    "nxdomain": "Domain not found",
    "no_mx": "No mail server",
    "timeout": "Unresolved",
    "error": "Unresolved",
}


def classify_mx(domain: str, mx_host: str | None, status: str = "ok") -> str:
    """Provider label for a domain given its lowest-preference MX host."""
    consumer = _CONSUMER_DOMAINS.get(domain)
    if consumer:
        return consumer
    if status != "ok":
        return _STATUS_LABELS.get(status, "Unresolved")
    if not mx_host:
        return "No mail server"
    host = mx_host.lower().rstrip(".")
    for needle, label in _MX_RULES:
        if needle in host:
            return label
    return "Other"


def domain_of(email: str) -> str | None:
    """Lowercased domain part, or None when the address has no usable one."""
    if not email or "@" not in email:
        return None
    dom = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    return dom or None


async def _lookup_one(domain: str, sem: asyncio.Semaphore, idx: int) -> tuple[str, str | None, str]:
    """(domain, mx_host, status) for one domain. Never raises."""
    import dns.asyncresolver
    import dns.resolver

    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [_RESOLVERS[idx % len(_RESOLVERS)]]
    resolver.timeout = _TIMEOUT
    resolver.lifetime = _TIMEOUT

    async with sem:
        for attempt in range(_ATTEMPTS):
            try:
                answer = await resolver.resolve(domain, "MX")
                # Lowest preference wins — that's the primary mail exchanger.
                best = min(answer, key=lambda r: r.preference)
                return domain, str(best.exchange).rstrip("."), "ok"
            except dns.resolver.NXDOMAIN:
                return domain, None, "nxdomain"
            except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
                return domain, None, "no_mx"
            except (dns.exception.Timeout, asyncio.TimeoutError):
                if attempt + 1 >= _ATTEMPTS:
                    return domain, None, "timeout"
            except Exception:
                if attempt + 1 >= _ATTEMPTS:
                    return domain, None, "error"
    return domain, None, "error"


async def resolve_domains(
    domains: Iterable[str],
    *,
    concurrency: int = 150,
    on_progress=None,
) -> dict[str, int]:
    """Resolve and cache every domain not already in email_domain_provider.

    Resumable by construction: cached domains are skipped, so re-running after
    an interruption only picks up what is missing. Returns per-status counts.
    """
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    wanted = sorted({d for d in domains if d})
    if not wanted:
        return {}

    # Skip what the cache already answers (and anything that failed only
    # transiently is left for an explicit retry pass, not re-done here).
    async with AsyncSessionLocal() as db:
        known = {
            r[0] for r in (await db.execute(
                sa_text("SELECT domain FROM email_domain_provider "
                        "WHERE domain = ANY(CAST(:d AS text[]))").bindparams(d=wanted)
            )).all()
        }
    todo = [d for d in wanted if d not in known]
    logger.info("email_providers: %d domains, %d cached, %d to resolve",
                len(wanted), len(known), len(todo))
    if not todo:
        return {"cached": len(known)}

    sem = asyncio.Semaphore(concurrency)
    counts: dict[str, int] = {"cached": len(known)}
    batch: list[tuple[str, str | None, str]] = []
    done = 0

    async def flush() -> None:
        nonlocal batch
        if not batch:
            return
        rows = [
            {"d": d, "p": classify_mx(d, mx, st), "m": mx, "s": st}
            for d, mx, st in batch
        ]
        async with AsyncSessionLocal() as db:
            await db.execute(sa_text("""
                INSERT INTO email_domain_provider (domain, provider, mx_host, status)
                VALUES (:d, :p, :m, :s)
                ON CONFLICT (domain) DO UPDATE
                   SET provider = EXCLUDED.provider,
                       mx_host  = EXCLUDED.mx_host,
                       status   = EXCLUDED.status,
                       resolved_at = now()
            """), rows)
            await db.commit()
        batch = []

    for chunk_start in range(0, len(todo), 2000):
        chunk = todo[chunk_start:chunk_start + 2000]
        results = await asyncio.gather(
            *(_lookup_one(d, sem, i) for i, d in enumerate(chunk))
        )
        for r in results:
            counts[r[2]] = counts.get(r[2], 0) + 1
            batch.append(r)
        await flush()
        done += len(chunk)
        if on_progress:
            on_progress(done, len(todo), counts)

    return counts
