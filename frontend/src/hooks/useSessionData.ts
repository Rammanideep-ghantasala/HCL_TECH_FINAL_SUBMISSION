import { useCallback, useEffect, useState } from "react";
import {
  addRecent,
  loadFeedback,
  loadRecent,
  removeFeedback,
  saveFeedback,
  saveRecent,
  upsertFeedback,
  type FeedbackRecord,
  type RecentSearch,
} from "../storage/session";

export interface SessionData {
  recent: RecentSearch[];
  feedback: FeedbackRecord[];
  rememberSearch: (entry: RecentSearch) => void;
  recordFeedback: (record: FeedbackRecord) => void;
  withdrawFeedback: (resultId: string) => void;
  clearAll: () => void;
}

/**
 * Recent searches and feedback for this browser tab. React state is the
 * working copy; sessionStorage is kept in step with it and read once at start.
 */
export function useSessionData(): SessionData {
  const [recent, setRecent] = useState<RecentSearch[]>(loadRecent);
  const [feedback, setFeedback] = useState<FeedbackRecord[]>(loadFeedback);

  useEffect(() => {
    saveRecent(recent);
  }, [recent]);

  useEffect(() => {
    saveFeedback(feedback);
  }, [feedback]);

  const rememberSearch = useCallback((entry: RecentSearch) => {
    setRecent((previous) => addRecent(previous, entry));
  }, []);

  const recordFeedback = useCallback((record: FeedbackRecord) => {
    setFeedback((previous) => upsertFeedback(previous, record));
  }, []);

  const withdrawFeedback = useCallback((resultId: string) => {
    setFeedback((previous) => removeFeedback(previous, resultId));
  }, []);

  const clearAll = useCallback(() => {
    setRecent([]);
    setFeedback([]);
  }, []);

  return { recent, feedback, rememberSearch, recordFeedback, withdrawFeedback, clearAll };
}
