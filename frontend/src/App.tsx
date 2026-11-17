import { useEffect, useRef, useState } from "react";
import { fetchExamples, fetchMeta, isAbortError } from "./api/client";
import type { ExampleTicket, MetaResponse, ResultCard } from "./api/types";
import { Header } from "./components/Header";
import { OutcomeArea } from "./components/OutcomeArea";
import { ResolutionForm } from "./components/ResolutionForm";
import { SessionPanel } from "./components/SessionPanel";
import { SummaryPanel } from "./components/SummaryPanel";
import { TicketForm, type FieldRefs } from "./components/TicketForm";
import { FALLBACK_DATA_NOTICE, FALLBACK_LIMITS } from "./constants";
import { TicketCacheProvider } from "./hooks/ticketCache";
import { useSearch } from "./hooks/useSearch";
import { useSessionData } from "./hooks/useSessionData";
import {
  escalationAddress,
  identityFrom,
  initialResolutionValues,
  saveLimitsFrom,
  type ResolutionField,
  type ResolutionValues,
} from "./state/resolution";
import {
  announcementFor,
  EMPTY_FORM,
  errorsAfterEdit,
  firstInvalidField,
  mapRejection,
  sameValues,
  toPayload,
  validateForm,
  type FieldErrors,
  type FieldName,
  type FormValues,
} from "./state/search";
import {
  buildFeedbackExport,
  downloadJson,
  newEventId,
  type FeedbackReason,
  type RecentSearch,
  type Verdict,
} from "./storage/session";

function samePayload(a: FormValues, b: FormValues): boolean {
  return JSON.stringify(toPayload(a)) === JSON.stringify(toPayload(b));
}

/** The resolution being recorded for one completed search, identified by its request_id. */
interface ResolutionDraft {
  requestId: string;
  /** Created once for this search result and sent with every save attempt from it. */
  submissionId: string;
  open: boolean;
  values: ResolutionValues;
  savedTicketId: string | null;
}

export function App() {
  const [form, setForm] = useState<FormValues>(EMPTY_FORM);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [metaFailed, setMetaFailed] = useState(false);
  const [examples, setExamples] = useState<ExampleTicket[]>([]);
  /** A one-off message for the live region about something other than a search. */
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState<ResolutionDraft | null>(null);

  const { state, runSearch, reset } = useSearch();
  const session = useSessionData();
  const recordButtonRef = useRef<HTMLButtonElement>(null);
  const resolutionHeadingRef = useRef<HTMLHeadingElement>(null);

  const subjectRef = useRef<HTMLInputElement>(null);
  const descriptionRef = useRef<HTMLTextAreaElement>(null);
  const productRef = useRef<HTMLInputElement>(null);
  const errorDetailRef = useRef<HTMLInputElement>(null);
  const categoryRef = useRef<HTMLSelectElement>(null);
  const searchButtonRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const fieldRefs: FieldRefs = {
    subject: subjectRef,
    description: descriptionRef,
    product: productRef,
    error_detail: errorDetailRef,
    category: categoryRef,
  };

  // Settings and examples are loaded once. Neither is needed for searching.
  useEffect(() => {
    const controller = new AbortController();
    fetchMeta(controller.signal).then(setMeta, (error: unknown) => {
      if (!isAbortError(error)) {
        setMetaFailed(true);
      }
    });
    fetchExamples(controller.signal).then(setExamples, () => {
      // Without examples the example area is simply not shown.
    });
    return () => controller.abort();
  }, []);

  const limits = meta?.limits ?? FALLBACK_LIMITS;
  const serverNotice = meta?.data_notice.trim() ?? "";
  const identity = identityFrom(meta);

  // A draft belongs to one completed search. Any other search state hides it.
  const activeDraft =
    draft !== null && state.phase === "done" && state.response.request_id === draft.requestId
      ? draft
      : null;

  const focusField = (field: FieldName) => {
    fieldRefs[field].current?.focus();
  };

  /** Runs one search and, if it is still the current one, records how it ended. */
  const execute = async (values: FormValues) => {
    setNotice(null);
    // A new search closes any resolution form for the previous result.
    setDraft(null);
    const outcome = await runSearch(values);
    if (outcome === null) {
      return;
    }

    session.rememberSearch({
      ...values,
      time: new Date().toISOString(),
      outcome: outcome.type === "done" ? outcome.response.status : "failed",
    });

    let invalidField: FieldName | null = null;
    if (outcome.type === "failed" && outcome.kind === "rejected" && outcome.failure) {
      const serverErrors = mapRejection(outcome.failure).fieldErrors;
      setFieldErrors(serverErrors);
      invalidField = firstInvalidField(serverErrors);
    }

    // Move focus to the outcome only if the agent has not moved on already.
    if (document.activeElement === searchButtonRef.current) {
      if (invalidField) {
        focusField(invalidField);
      } else {
        headingRef.current?.focus();
      }
    }
  };

  const handleSubmit = () => {
    const errors = validateForm(form, limits);
    const invalidField = firstInvalidField(errors);
    setFieldErrors(errors);
    if (invalidField) {
      setNotice("Not searched. Correct the marked field and press Search again.");
      focusField(invalidField);
      return;
    }
    void execute(form);
  };

  const handleChange = (field: FieldName, value: string) => {
    setForm((previous) => ({ ...previous, [field]: value }));
    setFieldErrors((previous) => errorsAfterEdit(previous, field));
  };

  const handleTopKChange = (count: 3 | 5) => {
    const values = { ...form, top_k: count };
    setForm(values);
    // Changing the count before the first search does not send a request.
    if (state.phase !== "idle") {
      const errors = validateForm(values, limits);
      setFieldErrors(errors);
      if (firstInvalidField(errors)) {
        reset();
      } else {
        void execute(values);
      }
    }
  };

  const handleClear = () => {
    reset();
    setDraft(null);
    setForm(EMPTY_FORM);
    setFieldErrors({});
    setNotice("The form and the results were cleared.");
  };

  const handleRetry = () => {
    if (state.phase !== "failed") {
      return;
    }
    // The Retry button disappears while searching, so focus moves to the heading first.
    headingRef.current?.focus();
    void execute(state.submitted);
  };

  const handleClearFilterAndSearch = () => {
    if (state.phase !== "done") {
      return;
    }
    setForm((previous) => ({ ...previous, category: "" }));
    setFieldErrors((previous) => errorsAfterEdit(previous, "category"));
    headingRef.current?.focus();
    void execute({ ...state.submitted, category: "" });
  };

  const fillForm = (values: FormValues, message: string) => {
    setForm(values);
    setFieldErrors({});
    setNotice(message);
  };

  const handleLoadExample = (example: ExampleTicket) => {
    fillForm(
      { ...EMPTY_FORM, top_k: form.top_k ?? 3, subject: example.subject, description: example.description },
      "An example was placed in the form. Press Search to run it.",
    );
  };

  const handleLoadRecent = (entry: RecentSearch) => {
    fillForm(
      {
        subject: entry.subject,
        description: entry.description,
        product: entry.product,
        error_detail: entry.error_detail,
        category: entry.category,
        top_k: entry.top_k ?? 3,
      },
      "A recent search was placed in the form. Press Search to run it.",
    );
  };

  const handleFeedback = (
    card: ResultCard,
    verdict: Verdict | null,
    reason: FeedbackReason | null,
  ) => {
    if (state.phase !== "done") {
      return;
    }
    if (verdict === null) {
      session.withdrawFeedback(card.result_id);
      return;
    }
    const { response, submitted } = state;
    session.recordFeedback({
      event_id: newEventId(),
      time: new Date().toISOString(),
      request_id: response.request_id,
      query_fingerprint: response.query_fingerprint,
      result_id: card.result_id,
      ticket_id: card.ticket.ticket_id,
      rank: card.rank,
      match_level: card.match_level,
      verdict,
      reason,
      results_shown: [...response.results, ...response.related].map((shown) => shown.ticket.ticket_id),
      status: response.status,
      mode: response.retrieval.mode,
      degraded: response.retrieval.degraded,
      index_version: response.retrieval.index_version,
      relevance_config_version: response.retrieval.relevance_config_version,
      model_id: response.retrieval.model_id,
      searched_input: submitted,
    });
  };

  const handleExport = (includeSearchedText: boolean) => {
    const exportedAt = new Date().toISOString();
    const stamp = exportedAt.replace(/[:.]/g, "-");
    downloadJson(
      `ticket-similarity-feedback-${stamp}.json`,
      buildFeedbackExport(session.feedback, includeSearchedText, exportedAt),
    );
    setNotice("The feedback export was prepared as a download.");
  };

  const handleClearSession = () => {
    session.clearAll();
    setNotice("Recent searches and feedback were removed from this tab.");
  };

  const handleRecordResolution = () => {
    if (state.phase !== "done" || state.response.status !== "no_strong_match") {
      return;
    }
    if (activeDraft?.open) {
      resolutionHeadingRef.current?.focus();
      return;
    }
    // The draft, and its submission id, are created once per search result and then reused.
    setDraft(
      activeDraft !== null
        ? { ...activeDraft, open: true }
        : {
            requestId: state.response.request_id,
            submissionId: newEventId(),
            open: true,
            values: initialResolutionValues(state.submitted, identity),
            savedTicketId: null,
          },
    );
  };

  /** Applies `change` only to the draft of `requestId`, so a late callback cannot touch a newer one. */
  const updateDraft = (requestId: string, change: (current: ResolutionDraft) => ResolutionDraft) => {
    setDraft((previous) =>
      previous !== null && previous.requestId === requestId ? change(previous) : previous,
    );
  };

  const resolutionForm =
    activeDraft !== null && activeDraft.open ? (
      <ResolutionForm
        values={activeDraft.values}
        submissionId={activeDraft.submissionId}
        savedTicketId={activeDraft.savedTicketId}
        limits={saveLimitsFrom(meta)}
        categories={meta?.categories ?? []}
        identity={identity}
        headingRef={resolutionHeadingRef}
        onChange={<K extends ResolutionField>(field: K, value: ResolutionValues[K]) =>
          updateDraft(activeDraft.requestId, (current) => ({
            ...current,
            values: { ...current.values, [field]: value },
          }))
        }
        onSaved={(ticketId) =>
          updateDraft(activeDraft.requestId, (current) => ({ ...current, savedTicketId: ticketId }))
        }
        onClose={() => {
          updateDraft(activeDraft.requestId, (current) => ({ ...current, open: false }));
          recordButtonRef.current?.focus();
        }}
        onAnnounce={setNotice}
      />
    ) : null;

  const identicalInFlight = state.phase === "loading" && samePayload(form, state.submitted);
  const formEdited = state.phase !== "idle" && !sameValues(form, state.submitted);

  return (
    <TicketCacheProvider>
      <a className="skip-link" href="#outcome-heading">
        Skip to the search outcome
      </a>
      <Header dataNotice={serverNotice !== "" ? serverNotice : FALLBACK_DATA_NOTICE} />
      <main className="workspace">
        <TicketForm
          values={form}
          errors={fieldErrors}
          limits={limits}
          categories={meta?.categories ?? []}
          examples={examples}
          metaFailed={metaFailed}
          searchUnavailable={meta?.retrieval.mode === "unavailable"}
          identicalInFlight={identicalInFlight}
          subjectRef={subjectRef}
          descriptionRef={descriptionRef}
          productRef={productRef}
          errorDetailRef={errorDetailRef}
          categoryRef={categoryRef}
          searchButtonRef={searchButtonRef}
          onChange={handleChange}
          onTopKChange={handleTopKChange}
          onSubmit={handleSubmit}
          onClear={handleClear}
          onLoadExample={handleLoadExample}
        />
        <OutcomeArea
          state={state}
          formEdited={formEdited}
          headingRef={headingRef}
          feedback={session.feedback}
          onRetry={handleRetry}
          onClearFilterAndSearch={handleClearFilterAndSearch}
          onGoToField={focusField}
          onFeedback={handleFeedback}
          summary={state.phase === "done" && !formEdited && state.response.results.length > 0
            && state.response.status === "useful_matches" ? (
            <SummaryPanel key={state.response.request_id} response={state.response}
              info={meta?.summarization} topK={state.submitted.top_k ?? 3} />
          ) : null}
          noMatch={{
            escalationEmail: escalationAddress(meta),
            saveEnabled: meta?.save_enabled !== false,
            savedTicketId: activeDraft?.savedTicketId ?? null,
            formOpen: activeDraft?.open ?? false,
            recordButtonRef,
            onRecordResolution: handleRecordResolution,
            form: resolutionForm,
          }}
        />
        <SessionPanel
          recent={session.recent}
          feedbackCount={session.feedback.length}
          onLoadRecent={handleLoadRecent}
          onExport={handleExport}
          onClear={handleClearSession}
        />
      </main>
      <div className="visually-hidden" role="status" aria-live="polite">
        {notice ?? announcementFor(state)}
      </div>
    </TicketCacheProvider>
  );
}
