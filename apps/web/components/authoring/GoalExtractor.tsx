"use client";
import { useState } from "react";
import { Sparkles } from "lucide-react";
import Button from "@/components/ui/Button";
import DraftCard from "@/components/authoring/DraftCard";
import { contextV2Api, type Draft } from "@/lib/api/contextV2";

/**
 * (E) Say what you want recorded; get an extractor that already found some.
 *
 * The Extractor Studio asks thirty-four questions — mechanism, source selector, emit nodes, emit
 * edges, per-attribute overrides — before telling you whether the idea was any good. This asks
 * one, and answers with output: the draft comes back having been dry-run against real text, so
 * the first thing a user sees is what it found, not a form.
 *
 * `content` is the sample the draft is previewed against. Passing it is what turns the result
 * from a plausible-looking template into a verified one — a draft that found nothing says so and
 * drops to LOW confidence, which is the outcome worth catching before it runs over a corpus.
 */

const EXAMPLES = [
  "track which vendors we owe money to",
  "pull out every deadline and who owns it",
  "find the risks and how severe each one is",
];

export default function GoalExtractor({
  content = "",
  filename = "",
  onSaved,
}: {
  /** Text the draft is previewed against. Without it the draft is built but unverified. */
  content?: string;
  filename?: string;
  onSaved?: (draft: Draft) => void;
}) {
  const [goal, setGoal] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [busy, setBusy] = useState(false);
  const [savingName, setSavingName] = useState("");
  const [savedNames, setSavedNames] = useState<string[]>([]);
  const [error, setError] = useState("");

  const run = async () => {
    const wanted = goal.trim();
    if (!wanted || busy) return;
    setBusy(true);
    setError("");
    setDrafts([]);
    try {
      const res = await contextV2Api.draft({
        kind: "extractor", goal: wanted, content: content || undefined,
        filename: filename || undefined,
      });
      setDrafts(res.drafts);
      if (res.drafts.length === 0) {
        setError("Nothing could be drafted from that. Try naming the thing you want recorded.");
      }
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  const save = async (draft: Draft) => {
    setSavingName(draft.name);
    setError("");
    try {
      await contextV2Api.saveDraft(draft);
      setSavedNames((prev) => [...prev, draft.name]);
      onSaved?.(draft);
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Could not save that extractor.");
    } finally {
      setSavingName("");
    }
  };

  return (
    <div className="space-y-3">
      <div className="bg-white dark:bg-stone-900 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs p-4 space-y-3">
        <div>
          <label htmlFor="extractor-goal" className="block text-sm font-semibold text-stone-900 dark:text-stone-100">
            What else should this remember?
          </label>
          <p className="text-sm text-stone-500 dark:text-stone-400 mt-0.5">
            Describe it the way you would to a colleague. No configuration needed.
          </p>
        </div>

        <div className="flex flex-col sm:flex-row gap-2">
          <input
            id="extractor-goal"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") run(); }}
            placeholder="track which vendors we owe money to"
            className="flex-1 rounded-lg border border-stone-200 dark:border-stone-700
                       bg-white dark:bg-stone-800 px-3 py-2 text-sm
                       text-stone-900 dark:text-stone-100 placeholder:text-stone-400
                       outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          />
          <Button variant="primary" icon={Sparkles} busy={busy}
                  disabled={!goal.trim()} onClick={run}>
            Draft it
          </Button>
        </div>

        {drafts.length === 0 && !busy && (
          <div className="flex flex-wrap gap-1.5">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                onClick={() => setGoal(example)}
                className="text-xs px-2.5 py-1 rounded-full border border-stone-200 dark:border-stone-700
                           text-stone-600 dark:text-stone-400 hover:bg-stone-50 dark:hover:bg-stone-800
                           transition-colors"
              >
                {example}
              </button>
            ))}
          </div>
        )}

        {!content && (
          <p className="text-xs text-stone-400 dark:text-stone-500">
            Nothing to test against here — the draft will be built but not run. Add data first to
            see what it actually finds.
          </p>
        )}

        {error && (
          <p className="text-sm text-red-700 dark:text-red-400">{error}</p>
        )}
      </div>

      {drafts.map((draft) => (
        <DraftCard
          key={draft.name}
          draft={draft}
          onSave={save}
          saving={savingName === draft.name}
          saved={savedNames.includes(draft.name)}
        />
      ))}
    </div>
  );
}
