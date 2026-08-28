"use client";

import { useEffect, useState } from "react";
import { api, setToken, type DemoIdentity, type Session } from "@/lib/api";
import { Logo } from "@/components/shell";

/** The way into the lab's side of BioIntake.
 *
 *  A token rather than a password: the people using this already carry credentials issued by the
 *  institution, and inventing a password store for a handful of staff would add a thing to lose
 *  without adding a thing to trust. The token is checked against the server before it is kept, so a
 *  mistyped one fails here rather than silently on the first action.
 */
export function SignIn() {
  const [token, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Present only where the deployment offers it; everywhere else this stays empty and the screen
  // is the token box alone.
  const [identities, setIdentities] = useState<DemoIdentity[]>([]);

  useEffect(() => {
    let live = true;
    api
      .demoIdentities()
      .then((list) => {
        if (live) setIdentities(list);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setToken(token.trim());
      const session: Session = await api.me();
      if (!session.user_id) throw new Error("no identity");
    } catch {
      setToken("");
      setError("That token was not accepted. Check it was copied whole, then try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center bg-bg px-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-surface p-6">
        <div className="flex items-center gap-2 text-accent">
          <Logo size={22} />
          <span className="text-lg font-semibold text-fg">BioIntake</span>
        </div>
        <div>
          <h1 className="text-base font-semibold text-fg">Sign in</h1>
          <p className="mt-1 text-sm text-fg-muted">
            Every acceptance, exception and rejection is recorded against the person who made it, so
            BioIntake needs to know who you are before it will show you a case.
          </p>
        </div>
        <label className="block text-sm">
          <span className="text-fg-muted">Access token</span>
          <input
            type="password"
            autoFocus
            autoComplete="off"
            value={token}
            onChange={(e) => setValue(e.target.value)}
            placeholder="bit_…"
            className="mt-1 w-full rounded border border-border bg-bg px-2 py-1.5 font-mono text-sm text-fg"
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-fail-fg">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy || !token.trim()}
          className="w-full rounded-md bg-accent px-3.5 py-2 text-sm font-semibold text-accent-fg transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Checking…" : "Sign in"}
        </button>
        {identities.length > 0 ? (
          /* One click per person rather than one shared account. The click is the point: you are
             signed in *as somebody*, and the case you then accept says so. Each button hands over
             that person's real token; nothing here bypasses authentication, and the roles still
             bite, so the coordinator is refused when they try to author a study. */
          <div className="space-y-2 border-t border-border pt-4">
            <p className="text-sm text-fg-muted">
              Reviewing this deployment? Sign in as one of the lab&apos;s staff. All data is synthetic.
            </p>
            {identities.map((who) => (
              <button
                key={who.user_id}
                type="button"
                onClick={() => setToken(who.token)}
                className="flex w-full items-baseline justify-between gap-3 rounded border border-border px-3 py-2 text-left text-sm transition hover:border-border-strong hover:bg-surface-2"
              >
                <span className="text-fg">{who.display_name}</span>
                <span className="font-mono text-[13px] text-fg-muted">
                  {who.role.toLowerCase().replace(/_/g, " ")}
                </span>
              </button>
            ))}
          </div>
        ) : null}
        {/* Deliberately not "check the API console": on a hosted deployment there is no console to
            check, and telling someone to look somewhere that does not exist is worse than saying
            nothing. Whoever runs the deployment is the answer in both cases. */}
        <p className="text-[13px] text-fg-muted">
          Tokens are issued by whoever administers this BioIntake, and are not recoverable once
          issued, a lost one is replaced, not looked up.
        </p>
      </form>
    </main>
  );
}
