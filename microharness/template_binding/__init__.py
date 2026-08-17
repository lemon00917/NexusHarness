"""Database-backed HTML and clinical-template binding workbench."""

from .config import (
    CONFIG_PATH,
    TemplateBindingDatabaseConfig,
    build_database_config,
    load_database_config,
    load_stored_database_config,
    save_database_config,
)
from .id_provider import IdProvider, IdProviderNotConfigured, SnowflakeIdProvider, get_id_provider
from .persistence import TemplateBindingCommitError, TemplateBindingCommitService
from .repository import (
    TemplateBindingConflictError,
    TemplateBindingRepository,
    TemplateBindingRepositoryError,
)
from .service import TemplateBindingAnalysisError, TemplateBindingAnalysisService
from .validator import BindingRecommendationValidator

__all__ = [
    "IdProvider",
    "IdProviderNotConfigured",
    "SnowflakeIdProvider",
    "CONFIG_PATH",
    "TemplateBindingDatabaseConfig",
    "TemplateBindingRepository",
    "TemplateBindingRepositoryError",
    "TemplateBindingConflictError",
    "TemplateBindingCommitError",
    "TemplateBindingCommitService",
    "TemplateBindingAnalysisError",
    "TemplateBindingAnalysisService",
    "BindingRecommendationValidator",
    "build_database_config",
    "get_id_provider",
    "load_database_config",
    "load_stored_database_config",
    "save_database_config",
]
