"""Configuration for attestation proxy service."""

from typing import Optional

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from sek8s_common.config import AuthConfig


class AttestationProxyConfig(AuthConfig):
    """Configuration for attestation proxy service.

    Requires auth fields to be configured.
    """

    allowed_validators_str: str = Field(..., alias="ALLOWED_VALIDATORS")
    miner_ss58: str = Field(..., alias="MINER_SS58")
    # Optional: the miner sr25519 seed. When set (new charts inject it from the
    # miner-credentials secret), the external proxy signs each response with the miner
    # hotkey as a release-candidate proof-of-possession. Absent (old charts running the
    # latest image) -> no signing, unchanged pass-through, so older VMs keep working.
    miner_seed: Optional[str] = Field(default=None, alias="MINER_SEED")

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )
