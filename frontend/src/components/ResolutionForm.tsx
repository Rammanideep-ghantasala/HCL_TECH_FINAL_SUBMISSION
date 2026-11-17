import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from "react";
import {
  ApiFailure,
  fetchStoredTicket,
  isAbortError,
  refreshIndex,
  saveResolvedTicket,
} from "../api/client";
import type { IndexingStatus, SaveLimits, SaveTicketResponse, StoredTicket } from "../api/types";
import { SAVE_TIMEOUT_MS } from "../constants";
import { useInvalidateTicket } from "../hooks/ticketCache";
import {
  classifySaveFailure,
  firstInvalidResolutionField,
  isRequired,
  mapSaveRejection,
  RESOLUTION_LABELS,
  resolutionErrorsAfterEdit,
  resolutionLimitFor,
  SAVE_FAILURE_ADVICE,
  SAVE_FAILURE_TITLES,
  toSaveRequest,
  UNKNOWN_ROOT_CAUSE,
  validateResolution,
  type ResolutionErrors,
  type ResolutionField,
  type ResolutionTextField,
  type ResolutionValues,
  type SaveFailureKind,
} from "../state/resolution";
import { Banner } from "./Banner";
import { CharCount, describedBy, FieldError } from "./TicketForm";

export const SAVED_AND_SEARCHABLE = "Ticket saved and available for future searches.";

/** Replaces the save-time message once a retry has made the ticket searchable. */
export const INDEXED_AFTER_RETRY = "Indexing was retried and the ticket is now in the search index.";

/** Shown for a ticket the server will not index until its content changes. */
export const SAVED_BUT_REJECTED = "Saved, but it cannot be indexed as written (it failed validation)";

const CATEGORY_LIST_ID = "resolution-category-options";

/** Thrown in place of an AbortError when the browser stopped a request that took too long. */
class RequestTimeout extends Error {}

interface SaveError {
  kind: SaveFailureKind;
  /** Server text, when the server sent any. */
  message: string;
  requestId: string | null;
}

type StoredState =
  | { status: "loading" }
  | { status: "loaded"; ticket: StoredTicket }
  | { status: "error"; message: string };

interface Conflict {
  ticketId: string;
  stored: StoredState;
}

interface SavedOutcome {
  ticketId: string;
  /** Shown only while the ticket is searchable; replaced once a retry made it searchable. */
  message: string;
  repeat: boolean;
  indexing: IndexingStatus;
  /** True only when the server reports this ticket in the live search index. */
  searchable: boolean;
  /** True when the server will not index this ticket until its content changes. */
  rejected: boolean;
  refreshing: boolean;
  refreshError: string | null;
}

/** The save area is always in exactly one of these states. */
type SaveStatus =
  | { phase: "editing" }
  /** `conflict` keeps the stored version on screen while the agent's choice is being sent. */
  | { phase: "saving"; conflict: Conflict | null }
  | { phase: "failed"; error: SaveError }
  | { phase: "conflict"; conflict: Conflict }
  | { phase: "saved"; outcome: SavedOutcome };

function fieldId(field: ResolutionField): string {
  return `resolution-${field}`;
}

function FieldLabel({ field }: { field: ResolutionField }) {
  const required = isRequired(field);
  return (
    <label htmlFor={fieldId(field)}>
      {RESOLUTION_LABELS[field]}{" "}
      <span className={required ? "field-tag field-tag-required" : "field-tag"}>
        {required ? "(required)" : "(optional)"}
      </span>
    </label>
  );
}

interface TextFieldProps {
  field: ResolutionTextField;
  value: string;
  error: string | undefined;
  max: number | null;
  hint?: string;
  multiline?: boolean;
  /** Never editable here (the signed-in identity). */
  readOnly?: boolean;
  /** Read-only for now, while a save is in flight. The field stays focusable. */
  locked?: boolean;
  listId?: string;
  register: (field: ResolutionField, element: HTMLElement | null) => void;
  onChange: (field: ResolutionTextField, value: string) => void;
  /** Extra controls shown under the input, such as a fill-in button. */
  children?: ReactNode;
}

function TextField({
  field,
  value,
  error,
  max,
  hint,
  multiline = false,
  readOnly = false,
  locked = false,
  listId,
  register,
  onChange,
  children,
}: TextFieldProps) {
  const id = fieldId(field);
  const hintId = `${id}-hint`;
  const countId = `${id}-count`;
  const errorId = `${id}-error`;
  const common = {
    id,
    name: field,
    value,
    readOnly: readOnly || locked,
    autoComplete: "off",
    "aria-invalid": error ? true : undefined,
    "aria-required": isRequired(field) && !readOnly ? true : undefined,
    "aria-describedby": describedBy(
      hint !== undefined && hintId,
      max !== null && countId,
      error !== undefined && errorId,
    ),
  };
  return (
    <div className="field">
      <FieldLabel field={field} />
      {multiline ? (
        <textarea
          {...common}
          rows={4}
          ref={(element) => register(field, element)}
          onChange={(event) => onChange(field, event.target.value)}
        />
      ) : (
        <input
          {...common}
          type="text"
          list={listId}
          ref={(element) => register(field, element)}
          onChange={(event) => onChange(field, event.target.value)}
        />
      )}
      {children}
      {hint !== undefined ? (
        <p id={hintId} className="hint">
          {hint}
        </p>
      ) : null}
      {max !== null ? <CharCount id={countId} length={value.length} max={max} /> : null}
      <FieldError id={errorId} message={error} />
    </div>
  );
}

function ConflictPanel({
  conflict,
  busy,
  onReplace,
  onSaveAsNew,
  onReload,
}: {
  conflict: Conflict;
  busy: boolean;
  onReplace: () => void;
  onSaveAsNew: () => void;
  onReload: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const { ticketId, stored } = conflict;
  const loading = stored.status === "loading";
  // Focus moves here when the conflict appears, and again when a newer stored version is fetched.
  useEffect(() => {
    headingRef.current?.focus();
  }, [loading]);

  return (
    <section className="banner banner-warning conflict" aria-labelledby="resolution-conflict-heading">
      <h4
        id="resolution-conflict-heading"
        ref={headingRef}
        tabIndex={-1}
        className="banner-heading"
      >
        <span className="banner-tag">Not saved</span>
        <span className="banner-title">Ticket {ticketId} already exists with different content</span>
      </h4>
      <div className="banner-body">
        <p>
          <strong>Nothing was saved.</strong> Compare the stored version with your form, then
          choose. The stored version is replaced only if you press &ldquo;Replace the stored
          version&rdquo;.
        </p>
        {stored.status === "loading" ? (
          <p className="muted">Loading the stored version of {ticketId}…</p>
        ) : null}
        {stored.status === "error" ? (
          <div className="lookup-error">
            <p>{stored.message}</p>
            <button type="button" className="button button-small" onClick={onReload}>
              Load the stored version again
            </button>
          </div>
        ) : null}
        {stored.status === "loaded" ? (
          <dl className="record stored-version">
            <dt>Subject</dt>
            <dd className="ticket-text">{stored.ticket.subject}</dd>
            <dt>Description</dt>
            <dd className="ticket-text">{stored.ticket.description}</dd>
            <dt>Resolution notes</dt>
            <dd className="ticket-text">{stored.ticket.resolution_notes}</dd>
            <dt>Revision</dt>
            <dd className="ticket-text">
              <code>{stored.ticket.revision}</code>
            </dd>
          </dl>
        ) : null}
        <div className="form-actions">
          {stored.status === "loaded" ? (
            <button
              type="button"
              className="button"
              aria-disabled={busy ? true : undefined}
              onClick={onReplace}
            >
              Replace the stored version
            </button>
          ) : null}
          <button
            type="button"
            className="button"
            aria-disabled={busy ? true : undefined}
            onClick={onSaveAsNew}
          >
            Save as a new ticket instead
          </button>
          {busy ? <span className="hint form-actions-note">Saving…</span> : null}
        </div>
      </div>
    </section>
  );
}

function SaveFailure({ error }: { error: SaveError }) {
  return (
    <Banner tone="error" tag="Not saved" title={SAVE_FAILURE_TITLES[error.kind]}>
      {error.message !== "" ? <p>{error.message}</p> : null}
      <p>{SAVE_FAILURE_ADVICE[error.kind]}</p>
      <p className="basis">Your entries are still in the form.</p>
      {error.requestId ? (
        <p className="basis">
          Request id (quote this when reporting the problem):{" "}
          <code className="request-id">{error.requestId}</code>
        </p>
      ) : null}
    </Banner>
  );
}

function SavedPanel({
  outcome,
  onRetryIndexing,
}: {
  outcome: SavedOutcome;
  onRetryIndexing: () => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  // Focus follows the outcome, including when a retry makes the ticket searchable.
  useEffect(() => {
    panelRef.current?.focus();
  }, [outcome.searchable, outcome.rejected]);

  let banner: ReactNode;
  if (outcome.searchable) {
    banner = (
      <Banner tone="info" tag="Saved" title={SAVED_AND_SEARCHABLE}>
        <p>
          Ticket ID: <strong className="ticket-text">{outcome.ticketId}</strong>
        </p>
        {outcome.message !== "" ? <p>{outcome.message}</p> : null}
        {outcome.repeat ? (
          <p className="basis">This form had been saved before, so nothing new was written.</p>
        ) : null}
      </Banner>
    );
  } else if (outcome.rejected) {
    // Retrying cannot help: the server indexes this ticket only after its content changes.
    banner = (
      <Banner tone="warning" tag="Saved, not searchable" title={SAVED_BUT_REJECTED}>
        <p>
          The ticket was saved under <strong className="ticket-text">{outcome.ticketId}</strong>.
        </p>
        {outcome.indexing.message !== "" ? <p>{outcome.indexing.message}</p> : null}
        <p>
          It is not available to future searches and stays out of them until its content changes.
          Retrying indexing will not change that.
        </p>
      </Banner>
    );
  } else {
    banner = (
      <Banner
        tone="warning"
        tag="Saved, not searchable yet"
        title={`Saved under ${outcome.ticketId}, but not yet searchable`}
      >
        <p>
          The ticket was saved under <strong className="ticket-text">{outcome.ticketId}</strong>.
        </p>
        <p>{outcome.indexing.message}</p>
        <p>It is not yet available to future searches.</p>
        {outcome.refreshError ? <p className="field-error">{outcome.refreshError}</p> : null}
        <div className="form-actions">
          <button
            type="button"
            className="button button-primary"
            aria-disabled={outcome.refreshing ? true : undefined}
            onClick={onRetryIndexing}
          >
            Retry indexing
          </button>
          {outcome.refreshing ? (
            <span className="hint form-actions-note">Retrying indexing…</span>
          ) : null}
        </div>
      </Banner>
    );
  }

  return (
    <div ref={panelRef} tabIndex={-1} className="save-result">
      {banner}
    </div>
  );
}

export interface ResolutionFormProps {
  values: ResolutionValues;
  /** Created once for this search result; sent with every save from this draft. */
  submissionId: string;
  /** Set once this draft was saved. */
  savedTicketId: string | null;
  limits: SaveLimits;
  categories: string[];
  /** Signed-in identity from the server. When set, "Resolved by" is fixed to it. */
  identity: string | null;
  headingRef: RefObject<HTMLHeadingElement | null>;
  onChange: <K extends ResolutionField>(field: K, value: ResolutionValues[K]) => void;
  onSaved: (ticketId: string) => void;
  onClose: () => void;
  /** Sends a message to the page's live region. */
  onAnnounce: (message: string) => void;
}

/** Records how a ticket with no historical match was investigated and resolved. */
export function ResolutionForm({
  values,
  submissionId,
  savedTicketId,
  limits,
  categories,
  identity,
  headingRef,
  onChange,
  onSaved,
  onClose,
  onAnnounce,
}: ResolutionFormProps) {
  const [errors, setErrors] = useState<ResolutionErrors>({});
  const [status, setStatus] = useState<SaveStatus>({ phase: "editing" });
  /** Guards against a second request before the first one has re-rendered the page. */
  const saveInFlightRef = useRef(false);
  const refreshInFlightRef = useRef(false);
  const elementsRef = useRef<Partial<Record<ResolutionField, HTMLElement | null>>>({});
  const controllersRef = useRef(new Set<AbortController>());
  /** Drops a cached stored ticket so its next view shows what this form saved. */
  const invalidateTicket = useInvalidateTicket();

  useEffect(() => {
    headingRef.current?.focus();
  }, [headingRef]);

  // Requests still running when the form goes away are stopped.
  useEffect(() => {
    const controllers = controllersRef.current;
    return () => {
      for (const controller of controllers) {
        controller.abort();
      }
    };
  }, []);

  const register = useCallback((field: ResolutionField, element: HTMLElement | null) => {
    elementsRef.current[field] = element;
  }, []);

  const focusField = (field: ResolutionField) => {
    elementsRef.current[field]?.focus();
  };

  /** Runs one request with a time limit. A timeout is thrown as RequestTimeout. */
  const withTimeout = async <T,>(run: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    const controller = new AbortController();
    controllersRef.current.add(controller);
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, SAVE_TIMEOUT_MS);
    try {
      return await run(controller.signal);
    } catch (error) {
      if (timedOut && isAbortError(error)) {
        throw new RequestTimeout();
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
      controllersRef.current.delete(controller);
    }
  };

  const loadStored = (ticketId: string) => {
    setStatus((previous) =>
      previous.phase === "conflict" && previous.conflict.ticketId === ticketId
        ? { phase: "conflict", conflict: { ticketId, stored: { status: "loading" } } }
        : previous,
    );
    withTimeout((signal) => fetchStoredTicket(ticketId, signal)).then(
      (ticket) => {
        setStatus((previous) =>
          previous.phase === "conflict" && previous.conflict.ticketId === ticketId
            ? { phase: "conflict", conflict: { ticketId, stored: { status: "loaded", ticket } } }
            : previous,
        );
      },
      (error: unknown) => {
        if (isAbortError(error)) {
          return;
        }
        setStatus((previous) =>
          previous.phase === "conflict" && previous.conflict.ticketId === ticketId
            ? {
                phase: "conflict",
                conflict: {
                  ticketId,
                  stored: {
                    status: "error",
                    message: `The stored version of ${ticketId} could not be loaded, so it cannot be replaced yet.`,
                  },
                },
              }
            : previous,
        );
      },
    );
  };

  const announceIndexState = (ticketId: string, searchable: boolean, rejected: boolean, retried: boolean) => {
    if (searchable) {
      onAnnounce(`${SAVED_AND_SEARCHABLE} Ticket ID ${ticketId}.`);
    } else if (rejected) {
      onAnnounce(
        `The ticket was saved under ${ticketId}, but it cannot be indexed as written (it failed validation).`,
      );
    } else if (retried) {
      onAnnounce(`${ticketId} is still not available to future searches.`);
    } else {
      onAnnounce(
        `The ticket was saved under ${ticketId}, but it is not yet available to future searches.`,
      );
    }
  };

  const handleSaved = (response: SaveTicketResponse) => {
    const ticketId = response.ticket.ticket_id;
    const rejected = response.ticket.index_status === "rejected";
    const searchable = response.indexing.status === "indexed" && !rejected;
    // Any copy of this ticket fetched earlier on this page is now out of date.
    invalidateTicket(ticketId);
    onSaved(ticketId);
    setErrors({});
    setStatus({
      phase: "saved",
      outcome: {
        ticketId,
        message: response.message,
        repeat: response.repeat_submission,
        indexing: response.indexing,
        searchable,
        rejected,
        refreshing: false,
        refreshError: null,
      },
    });
    announceIndexState(ticketId, searchable, rejected, false);
  };

  const handleFailure = (error: unknown, sent: ResolutionValues) => {
    if (error instanceof RequestTimeout) {
      setStatus({ phase: "failed", error: { kind: "timeout", message: "", requestId: null } });
      onAnnounce(`Not saved. ${SAVE_FAILURE_TITLES.timeout}.`);
      return;
    }
    if (!(error instanceof ApiFailure)) {
      setStatus({ phase: "failed", error: { kind: "server", message: "", requestId: null } });
      onAnnounce(`Not saved. ${SAVE_FAILURE_TITLES.server}.`);
      return;
    }

    const ticketId = sent.ticket_id.trim();
    if (error.status === 409 && error.code === "ticket_conflict" && ticketId !== "") {
      setStatus({ phase: "conflict", conflict: { ticketId, stored: { status: "loading" } } });
      onAnnounce(`Not saved. Ticket ${ticketId} already exists with different content.`);
      loadStored(ticketId);
      return;
    }

    const kind = classifySaveFailure(error);
    const messages = [error.message];
    if (kind === "rejected") {
      const mapped = mapSaveRejection(error);
      messages.push(...mapped.other);
      const invalid = firstInvalidResolutionField(mapped.fieldErrors);
      if (invalid) {
        setErrors(mapped.fieldErrors);
        focusField(invalid);
      }
    }
    setStatus({
      phase: "failed",
      error: {
        kind,
        message: messages.filter((text) => text.trim() !== "").join(" "),
        requestId: error.requestId,
      },
    });
    onAnnounce(`Not saved. ${SAVE_FAILURE_TITLES[kind]}.`);
  };

  /** Sends one save. Ignored while another save is in flight or after this draft was saved. */
  const save = async (
    toSend: ResolutionValues,
    expectedRevision: string | null,
    carried: Conflict | null,
  ) => {
    if (
      saveInFlightRef.current ||
      status.phase === "saving" ||
      status.phase === "saved" ||
      savedTicketId !== null
    ) {
      return;
    }
    const found = validateResolution(toSend, limits, identity);
    const invalid = firstInvalidResolutionField(found);
    setErrors(found);
    if (invalid) {
      if (status.phase === "failed") {
        setStatus({ phase: "editing" });
      }
      onAnnounce("Not saved. Correct the marked fields, then press Save resolved ticket again.");
      focusField(invalid);
      return;
    }

    saveInFlightRef.current = true;
    setStatus({ phase: "saving", conflict: carried });
    onAnnounce("Saving the resolved ticket…");
    try {
      const payload = toSaveRequest(toSend, submissionId, expectedRevision, identity);
      const response = await withTimeout((signal) => saveResolvedTicket(payload, signal));
      handleSaved(response);
    } catch (error) {
      if (isAbortError(error)) {
        // The form was closed or replaced by a new search; nothing is shown.
        return;
      }
      handleFailure(error, toSend);
    } finally {
      saveInFlightRef.current = false;
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void save(values, null, null);
  };

  /** True while a save is in flight: the form is read-only, because edits made now would not be sent. */
  const isLocked = () => saveInFlightRef.current || status.phase === "saving";

  const handleTextChange = (field: ResolutionTextField, value: string) => {
    if (isLocked()) {
      return;
    }
    onChange(field, value);
    setErrors((previous) => resolutionErrorsAfterEdit(previous, field));
    // A different Ticket ID no longer refers to the stored version on screen.
    if (field === "ticket_id" && status.phase === "conflict") {
      setStatus({ phase: "editing" });
    }
  };

  const handleReplace = () => {
    if (status.phase !== "conflict" || status.conflict.stored.status !== "loaded") {
      return;
    }
    if (values.ticket_id.trim() !== status.conflict.ticketId) {
      return;
    }
    void save(values, status.conflict.stored.ticket.revision, status.conflict);
  };

  const handleSaveAsNew = () => {
    if (status.phase !== "conflict") {
      return;
    }
    const withoutId: ResolutionValues = { ...values, ticket_id: "" };
    onChange("ticket_id", "");
    void save(withoutId, null, status.conflict);
  };

  const handleReloadStored = () => {
    if (status.phase === "conflict") {
      loadStored(status.conflict.ticketId);
    }
  };

  const handleRetryIndexing = async () => {
    if (status.phase !== "saved" || status.outcome.refreshing || refreshInFlightRef.current) {
      return;
    }
    const ticketId = status.outcome.ticketId;
    refreshInFlightRef.current = true;
    const update = (change: Partial<SavedOutcome>) =>
      setStatus((previous) =>
        previous.phase === "saved"
          ? { phase: "saved", outcome: { ...previous.outcome, ...change } }
          : previous,
      );
    update({ refreshing: true, refreshError: null });
    onAnnounce("Retrying indexing…");
    try {
      const result = await withTimeout((signal) => refreshIndex(signal));
      const rejected = (result.rejected_ticket_ids ?? []).includes(ticketId);
      const searchable =
        result.indexing.status === "indexed" &&
        !result.unindexed_ticket_ids.includes(ticketId) &&
        !rejected;
      // The ticket's index state changed (or may have): a cached copy would show the old one.
      invalidateTicket(ticketId);
      update({
        indexing: result.indexing,
        searchable,
        rejected,
        refreshing: false,
        // The save-time message described the failed indexing; it no longer applies.
        ...(searchable ? { message: INDEXED_AFTER_RETRY } : {}),
      });
      announceIndexState(ticketId, searchable, rejected, true);
    } catch (error) {
      if (isAbortError(error)) {
        return;
      }
      const message =
        error instanceof RequestTimeout
          ? "Indexing was not retried: the request took too long."
          : error instanceof ApiFailure && error.kind === "network"
            ? "Indexing was not retried: the service could not be reached."
            : "Indexing was not retried: the service reported a problem.";
      update({
        refreshing: false,
        refreshError: `${message} The ticket is still saved under ${ticketId}. You can try again.`,
      });
      onAnnounce(message);
    } finally {
      refreshInFlightRef.current = false;
    }
  };

  const saving = status.phase === "saving";
  const conflict =
    status.phase === "conflict" ? status.conflict : status.phase === "saving" ? status.conflict : null;
  const limitOf = (field: ResolutionTextField) => resolutionLimitFor(field, limits);

  let body: ReactNode;
  if (status.phase === "saved") {
    body = <SavedPanel outcome={status.outcome} onRetryIndexing={() => void handleRetryIndexing()} />;
  } else if (savedTicketId !== null) {
    body = (
      <p className="recorded">
        <strong>Recorded as {savedTicketId}</strong>
      </p>
    );
  } else {
    body = (
      <>
        <p className="hint">
          Subject, description, product, error details and category were copied from the search
          you ran. Check them before saving. Fields marked (required) must be filled in.
        </p>
        <form className="ticket-form resolution-form" onSubmit={handleSubmit} noValidate>
          <TextField
            field="ticket_id"
            value={values.ticket_id}
            error={errors.ticket_id}
            max={null}
            hint="Leave blank to generate a new ID."
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="subject"
            value={values.subject}
            error={errors.subject}
            max={limitOf("subject")}
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="description"
            value={values.description}
            error={errors.description}
            max={limitOf("description")}
            multiline
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="category"
            value={values.category}
            error={errors.category}
            max={limitOf("category")}
            hint="Choose a listed category or type one."
            listId={CATEGORY_LIST_ID}
            register={register}
            onChange={handleTextChange}
          >
            <datalist id={CATEGORY_LIST_ID}>
              {categories.map((category) => (
                <option key={category} value={category} />
              ))}
            </datalist>
          </TextField>
          <TextField
            field="product"
            value={values.product}
            error={errors.product}
            max={limitOf("product")}
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="error_detail"
            value={values.error_detail}
            error={errors.error_detail}
            max={limitOf("error_detail")}
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="investigation"
            value={values.investigation}
            error={errors.investigation}
            max={limitOf("investigation")}
            hint="What was checked and what was found."
            multiline
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="root_cause"
            value={values.root_cause}
            error={errors.root_cause}
            max={limitOf("root_cause")}
            hint="If the cause was not found, press Unknown."
            multiline
            register={register}
            onChange={handleTextChange}
          >
            <button
              type="button"
              className="button button-small fill-button"
              onClick={() => handleTextChange("root_cause", UNKNOWN_ROOT_CAUSE)}
            >
              Unknown
            </button>
          </TextField>
          <TextField
            field="resolution_steps"
            value={values.resolution_steps}
            error={errors.resolution_steps}
            max={limitOf("resolution_steps")}
            hint="The actions that fixed the issue."
            multiline
            register={register}
            onChange={handleTextChange}
          />
          <TextField
            field="verification"
            value={values.verification}
            error={errors.verification}
            max={limitOf("verification")}
            hint="How you confirmed it was resolved."
            multiline
            register={register}
            onChange={handleTextChange}
          />
          {identity !== null ? (
            <TextField
              field="resolved_by"
              value={identity}
              error={errors.resolved_by}
              max={null}
              hint="Filled in from your signed-in identity. It cannot be changed here."
              readOnly
              register={register}
              onChange={() => undefined}
            />
          ) : (
            <TextField
              field="resolved_by"
              value={values.resolved_by}
              error={errors.resolved_by}
              max={limitOf("resolved_by")}
              register={register}
              onChange={handleTextChange}
            />
          )}
          <TextField
            field="tags"
            value={values.tags}
            error={errors.tags}
            max={null}
            hint={`Separate tags with commas. Up to ${limits.max_tags} tags of at most ${limits.tag_max_chars} characters each.`}
            register={register}
            onChange={handleTextChange}
          />

          <div className="field">
            <div className="checkbox-row">
              <input
                id={fieldId("confirm_resolved")}
                ref={(element) => register("confirm_resolved", element)}
                name="confirm_resolved"
                type="checkbox"
                checked={values.confirm_resolved}
                onChange={(event) => {
                  onChange("confirm_resolved", event.target.checked);
                  setErrors((previous) => resolutionErrorsAfterEdit(previous, "confirm_resolved"));
                }}
                aria-invalid={errors.confirm_resolved ? true : undefined}
                aria-describedby={describedBy(
                  errors.confirm_resolved !== undefined && `${fieldId("confirm_resolved")}-error`,
                )}
              />
              <FieldLabel field="confirm_resolved" />
            </div>
            <FieldError
              id={`${fieldId("confirm_resolved")}-error`}
              message={errors.confirm_resolved}
            />
          </div>

          <div className="form-actions">
            <button
              type="submit"
              className="button button-primary"
              aria-disabled={saving ? true : undefined}
            >
              Save resolved ticket
            </button>
            {saving ? (
              <span className="hint form-actions-note">Saving…</span>
            ) : (
              <button type="button" className="button" onClick={onClose}>
                Close form
              </button>
            )}
          </div>
        </form>

        {conflict ? (
          <ConflictPanel
            key={conflict.ticketId}
            conflict={conflict}
            busy={saving}
            onReplace={handleReplace}
            onSaveAsNew={handleSaveAsNew}
            onReload={handleReloadStored}
          />
        ) : null}
        {status.phase === "failed" ? <SaveFailure error={status.error} /> : null}
      </>
    );
  }

  return (
    <section className="resolution-panel" id="resolution-form" aria-labelledby="resolution-heading">
      <h3 id="resolution-heading" ref={headingRef} tabIndex={-1} className="section-heading">
        Record the resolution
      </h3>
      {body}
    </section>
  );
}
