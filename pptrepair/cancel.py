"""Coordinated cancellation for long-running scan / repair-all callbacks.

Raising :class:`OperationCancelled` from any progress callback passed to
:func:`pptrepair.scan.scan_paths` or :func:`pptrepair.batch.repair_paths`
is the supported way for a caller (CLI, GUI, ...) to abort a run that is
in progress; see :class:`OperationCancelled` for the exact contract.
"""

from __future__ import annotations


class OperationCancelled(Exception):
    """Raised by a progress callback to cooperatively cancel a run.

    Callers of :func:`pptrepair.scan.scan_paths` and
    :func:`pptrepair.batch.repair_paths` may raise this exception (or a
    subclass of it) from ``progress``, ``repair_progress``,
    ``on_download`` or ``material_progress`` to abort the run that is
    currently in progress. Neither function catches callback exceptions:
    they propagate unmodified to the caller, and that propagation is the
    documented, official contract for cooperative cancellation.

    Any temporary directory the core opens along the way (e.g. while
    mining archive material for donor candidates) is a context manager,
    so it is always cleaned up whether the run completes normally or is
    cancelled. Artifacts and report files already written before the
    cancellation point are left in place -- nothing already produced is
    rolled back.
    """
