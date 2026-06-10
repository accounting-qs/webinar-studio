"""
Webinar Studio — SQLAlchemy ORM Models
Re-exports all models from domain-specific modules so existing imports work unchanged.
"""

from db.models._common import gen_uuid  # noqa: F401
from db.models.users import User  # noqa: F401
from db.models.competitors import Competitor, ScrapeJob, CompetitorAd  # noqa: F401
from db.models.content import (  # noqa: F401
    CreativeConcept, GeneratedOutput, GeneratedOutputVersion,
    ChatSession, CopyFeedbackLog, MonitoringRun, WhatsAppSession,
)
from db.models.brain import (  # noqa: F401
    CopywritingPrinciple, UniversalBrain, FormatBrain,
    ContentRun, ContentPiece, ContentPieceVersion,
    BrainUpdate, SourceExample, CaseStudy,
)
from db.models.outreach import (  # noqa: F401
    OutreachBucket, BucketCopy, OutreachSender,
    Webinar, WebinarListAssignment, CopyUsageLog,
    BucketCopyGenerationJob, ContactReleaseLog, WebinarListExportJob,
)
from db.models.uploads import (  # noqa: F401
    UploadHistory, ContactCustomField, Contact,
)
from db.models.costs import CostLog  # noqa: F401
from db.models.connectors import (  # noqa: F401
    ConnectorCredential, WebinarGeekWebinar, WebinarGeekSubscriber,
)
from db.models.ghl import (  # noqa: F401
    GHLContact, GHLOpportunity, GHLSyncRun, GHLSyncSettings, GHLWebinarStats,
)
from db.models.blocklist import BlocklistEntry  # noqa: F401
from db.models.webinar_calendar import (  # noqa: F401
    WebinarCalendarUpload, WebinarCalendarInvite, CalendarAccountSender,
    WebinarNonjoinerInvite,
)

# Re-export Base so `from db.models import Base` keeps working
from db.base import Base  # noqa: F401
