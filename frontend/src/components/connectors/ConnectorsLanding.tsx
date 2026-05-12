"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  fetchWgStatus,
  fetchOpenAiStatus,
  fetchAnthropicStatus,
  fetchGhlConnectorStatus,
  type WgCredentialStatus,
  type OpenAiCredentialStatus,
  type AnthropicCredentialStatus,
  type GhlCredentialStatus,
} from "@/lib/api";

type Status = "connected" | "not_connected" | "loading";

export function ConnectorsLanding() {
  const [wg, setWg] = useState<Status>("loading");
  const [openai, setOpenai] = useState<Status>("loading");
  const [anthropic, setAnthropic] = useState<Status>("loading");
  const [ghl, setGhl] = useState<Status>("loading");

  useEffect(() => {
    fetchWgStatus()
      .then((s: WgCredentialStatus) => setWg(s.configured ? "connected" : "not_connected"))
      .catch(() => setWg("not_connected"));
    fetchOpenAiStatus()
      .then((s: OpenAiCredentialStatus) => setOpenai(s.configured ? "connected" : "not_connected"))
      .catch(() => setOpenai("not_connected"));
    fetchAnthropicStatus()
      .then((s: AnthropicCredentialStatus) => setAnthropic(s.configured ? "connected" : "not_connected"))
      .catch(() => setAnthropic("not_connected"));
    fetchGhlConnectorStatus()
      .then((s: GhlCredentialStatus) => setGhl(s.configured ? "connected" : "not_connected"))
      .catch(() => setGhl("not_connected"));
  }, []);

  return (
    <div className="px-6 py-6 max-w-[1400px]">
      <header className="mb-6">
        <div className="flex items-center gap-2 mb-1">
          <svg className="w-5 h-5 text-zinc-700 dark:text-zinc-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
          <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100 tracking-tight">Connectors</h1>
        </div>
        <p className="text-sm text-zinc-500">
          Manage integrations and AI providers that power your campaigns.
        </p>
      </header>

      <Section
        title="Integrations"
        subtitle="Webinar platforms and third-party services."
      >
        <ConnectorCard
          href="/connectors/webinargeek"
          name="WebinarGeek"
          description="Connect your WebinarGeek account to pull broadcasts and subscribers."
          status={wg}
          icon={
            <svg className="w-5 h-5 text-violet-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <polygon points="5 3 19 12 5 21 5 3" />
            </svg>
          }
        />
        <ConnectorCard
          href="/connectors/ghl"
          name="GoHighLevel"
          description="Connect your GHL location to sync contacts and opportunities for the Statistics dashboard."
          status={ghl}
          icon={
            <svg className="w-5 h-5 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          }
        />
      </Section>

      <Section
        title="AI Providers"
        subtitle="Used to extract case studies from URLs and (later) generate copy."
      >
        <ConnectorCard
          href="/connectors/openai"
          name="OpenAI"
          description="Powers the case-study URL importer in Copy Brain. Uses gpt-4o-mini (~$0.0002 per import)."
          status={openai}
          icon={
            <svg className="w-5 h-5 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
          }
        />
        <ConnectorCard
          href="/connectors/anthropic"
          name="Anthropic (Claude)"
          description="Powers the chat assistant on the Statistics page. Uses claude-opus-4-7 with adaptive thinking + prompt caching."
          status={anthropic}
          icon={
            <svg className="w-5 h-5 text-orange-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
            </svg>
          }
        />
      </Section>
    </div>
  );
}

function Section(props: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{props.title}</h2>
      <p className="text-xs text-zinc-500 mb-3">{props.subtitle}</p>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {props.children}
      </div>
    </section>
  );
}

function ConnectorCard(props: {
  href: string;
  name: string;
  description: string;
  status: Status;
  icon: React.ReactNode;
}) {
  return (
    <Link
      href={props.href}
      className="group rounded-xl border border-zinc-200 dark:border-zinc-800/60 bg-white dark:bg-zinc-900/40 p-4 hover:border-violet-300 dark:hover:border-violet-500/40 hover:shadow-sm transition-all"
    >
      <div className="flex items-center gap-2 mb-2">
        <div className="w-9 h-9 rounded-md bg-zinc-100 dark:bg-zinc-800/60 flex items-center justify-center">
          {props.icon}
        </div>
        <h3 className="text-sm font-bold text-zinc-900 dark:text-zinc-100 flex-1">{props.name}</h3>
        <StatusBadge status={props.status} />
      </div>
      <p className="text-xs text-zinc-500 leading-relaxed mb-3 min-h-[3em]">
        {props.description}
      </p>
      <div className="flex items-center justify-end text-xs text-zinc-400 group-hover:text-violet-500 transition-colors">
        <span>Manage</span>
        <svg className="w-4 h-4 ml-1" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
        </svg>
      </div>
    </Link>
  );
}

function StatusBadge({ status }: { status: Status }) {
  if (status === "loading") {
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60">
        …
      </span>
    );
  }
  if (status === "connected") {
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-emerald-500/15 text-emerald-500 border-emerald-500/30">
        Connected
      </span>
    );
  }
  return (
    <span className="px-2 py-0.5 rounded text-[10px] font-semibold border bg-zinc-100 dark:bg-zinc-800/60 text-zinc-500 border-zinc-200 dark:border-zinc-700/60">
      Not connected
    </span>
  );
}
