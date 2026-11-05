"""Stable reasons and operator guidance for fleet-blocking state."""

from __future__ import annotations

from dataclasses import asdict, dataclass


LINKEDIN_CHALLENGE = "linkedin_challenge"
LINKEDIN_SIGNED_OUT = "linkedin_signed_out"
LINKEDIN_IDENTITY_UNOBSERVABLE = "linkedin_identity_unobservable"
LINKEDIN_ACCOUNT_MISMATCH = "linkedin_account_mismatch"
LINKEDIN_CLEAR_FAILED = "linkedin_clear_failed"
WORKER_OPERATION_FAILED = "worker_operation_failed"
WORKER_OPERATION_ABANDONED = "worker_operation_abandoned"
WORKER_EXITED = "worker_exited"
WORKER_CONFIGURATION_INVALID = "worker_configuration_invalid"
CAPTURE_WORKER_LOST = "capture_worker_lost"
MISSING_FROM_DECLARATION = "missing_from_declaration"
MANUAL_HOLD = "manual_hold"
WORKER_QUARANTINED = "worker_quarantined"
LEGACY_ATTENTION = "legacy_attention"


@dataclass(frozen=True)
class AttentionGuidance:
    code: str
    title: str
    hint: str
    runbook_anchor: str

    def projection(self) -> dict[str, str]:
        return asdict(self)


def _item(code: str, title: str, hint: str) -> AttentionGuidance:
    return AttentionGuidance(
        code=code, title=title, hint=hint,
        runbook_anchor=code.replace("_", "-"),
    )


GUIDANCE = {
    item.code: item for item in (
        _item(
            LINKEDIN_CHALLENGE,
            "LinkedIn challenge needs a human",
            "Open this Chrome profile, resolve the LinkedIn challenge, then Verify.",
        ),
        _item(
            LINKEDIN_SIGNED_OUT,
            "LinkedIn session signed out",
            "Open this Chrome profile, sign in as the assigned account, then Verify.",
        ),
        _item(
            LINKEDIN_IDENTITY_UNOBSERVABLE,
            "LinkedIn identity not observable",
            "Open /in/me/ in this Chrome profile, restore a normal signed-in "
            "profile page for the assigned account, then Verify.",
        ),
        _item(
            LINKEDIN_ACCOUNT_MISMATCH,
            "LinkedIn account mismatch",
            "Compare /in/me/ with the assigned name and URL; Verify only if both "
            "match, otherwise Replace or Clear.",
        ),
        _item(
            LINKEDIN_CLEAR_FAILED,
            "LinkedIn session did not clear",
            "Open this Chrome profile, sign out of LinkedIn manually, close Chrome, "
            "then retry Clear.",
        ),
        _item(
            WORKER_OPERATION_FAILED,
            "Worker operation failed",
            "Save the worker-runner logs, inspect /in/me/, then use Verify for a "
            "correct session or Clear for any other session state.",
        ),
        _item(
            WORKER_OPERATION_ABANDONED,
            "Worker operation was abandoned",
            "Restore control and the worker supervisor, inspect /in/me/, then Verify "
            "a correct session or Clear and reassign it.",
        ),
        _item(
            WORKER_CONFIGURATION_INVALID,
            "Worker host configuration is invalid",
            "Read the exact reason in the detail beside this; fix that "
            "setting in the supervisor host's environment, restart the "
            "supervisor, then Assign or Verify again.",
        ),
        _item(
            WORKER_EXITED,
            "Worker runner exited",
            "The supervisor restarts this runner automatically and the hold "
            "clears after its next heartbeat. If it stays held, save the "
            "runner logs; the exit or startup error there is the fault.",
        ),
        _item(
            CAPTURE_WORKER_LOST,
            "Capture supervisor was lost",
            "Clears itself once the runner reports in again. If the heartbeat "
            "is already current the job merely stalled and this needs nothing; "
            "if it is stale, save the worker-runner logs and restart.",
        ),
        _item(
            MISSING_FROM_DECLARATION,
            "Worker missing from supervisor declaration",
            "Restore this exact worker id and profile in supervisor.json and restart; if "
            "removal was intentional, keep it disabled and report the worker id.",
        ),
        _item(
            MANUAL_HOLD,
            "Account held by an operator",
            "Identify who placed the hold and why; after they approve release, set "
            "the account to Enabled and Verify it if assigned.",
        ),
        _item(
            WORKER_QUARANTINED,
            "Worker quarantined",
            "Keep this worker disabled; give its id, detail, and saved profile to a "
            "maintainer because this UI has no quarantine-release action.",
        ),
        _item(
            LEGACY_ATTENTION,
            "Unclassified earlier attention state",
            "Assigned profile: click Verify. If it fails, follow the new reason "
            "shown. Use Clear only to sign out and unassign it. If nothing is "
            "assigned, report the resource id and saved detail.",
        ),
    )
}


def guidance(code: str) -> AttentionGuidance:
    try:
        return GUIDANCE[code]
    except KeyError:
        raise ValueError(f"unknown operator attention code {code!r}") from None
