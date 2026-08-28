"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSyncExternalStore } from "react";
import { currentToken, subscribeSession } from "@/lib/api";
import { SignIn } from "@/components/sign-in";
import { SessionBadge } from "@/components/ui";

/* ------------------------------------------------------------------------------------------------
   The application shell.

   A slim icon rail rather than a labelled sidebar, which is Benchling's arrangement and for the same
   reason: this product's real content is dense grids, twelve specimens by seven checks, a manifest of
   four hundred rows, and every pixel spent on persistent navigation is a pixel taken from the table.
   The rail is ~56px and never moves; the working surfaces stay white and read as paper against it.

   Labels are not hidden behind hover alone. Each item carries a visible caption under its glyph, because
   a coordinator using this twice a week should not have to learn an icon vocabulary, the single most
   common complaint in reviews of this category of software is that nobody can find anything.
------------------------------------------------------------------------------------------------ */


/* The mark: a cryovial, drawn as an outline with its contents filled to a level.
   A monogram in a rounded square is what every side project ships; this is the object the whole product is
   about, it is legible at 20px, and the filled level doubles as the one idea worth signalling, something
   came in, and it is being accounted for. */
export function Logo({ size = 22, withWordmark = false }: { size?: number; withWordmark?: boolean }) {
  const mark = (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden focusable="false">
      {/* contents, filled to a level */}
      <path d="M9.2 12.1h5.6v3.9a2.8 2.8 0 0 1-5.6 0z" fill="var(--accent)" />
      {/* cap and body */}
      <path
        d="M7.9 3.4h8.2M9.2 3.4v12.6a2.8 2.8 0 0 0 5.6 0V3.4"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* the level itself, the one line that says "measured, not guessed" */}
      <path d="M9.2 12.1h5.6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
  if (!withWordmark) return mark;
  return (
    <span className="flex items-center gap-2">
      {mark}
      <span className="text-[15px] font-semibold tracking-tight">
        Bio<span className="text-accent">Intake</span>
      </span>
    </span>
  );
}

type Item = { href: string; label: string; glyph: React.ReactNode; match: (p: string) => boolean };

function Glyph({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" className="size-5" aria-hidden>
      <path d={d} />
    </svg>
  );
}

const ITEMS: Item[] = [
  {
    href: "/",
    label: "Queue",
    glyph: <Glyph d="M4 6h16M4 12h16M4 18h10" />,
    match: (p) => p === "/" || p.startsWith("/cases"),
  },
  {
    href: "/receive",
    label: "Receive",
    glyph: <Glyph d="M3 8l9-5 9 5v8l-9 5-9-5V8zM3 8l9 5 9-5M12 13v8" />,
    match: (p) => p.startsWith("/receive"),
  },
  {
    href: "/announce",
    label: "Announce",
    glyph: <Glyph d="M4 9v6h4l5 4V5L8 9H4zM17 9a4 4 0 010 6" />,
    match: (p) => p.startsWith("/announce"),
  },
  {
    href: "/lab",
    label: "Lab",
    glyph: <Glyph d="M9 3v6l-5 9a2 2 0 002 3h12a2 2 0 002-3l-5-9V3M9 3h6M7 15h10" />,
    match: (p) => p.startsWith("/lab"),
  },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const token = useSyncExternalStore(subscribeSession, currentToken, () => "");
  // The sender portal is a public page reached from an email by someone who does not work here. It gets
  // no navigation, because there is nowhere else they are allowed to go.
  if (pathname.startsWith("/portal")) return <>{children}</>;
  if (!token) return <SignIn />;
  return (
    <div className="flex min-h-dvh">
      <nav
        aria-label="Sections"
        className="rail sticky top-0 flex h-dvh w-16 shrink-0 flex-col items-center gap-0.5 border-r border-black/40 py-2"
      >
        <Link
          href="/"
          className="mb-3 flex size-9 items-center justify-center text-rail-fg-strong transition hover:opacity-80"
          aria-label="BioIntake home"
        >
          <Logo size={24} />
        </Link>
        {ITEMS.map((it) => {
          const active = it.match(pathname);
          return (
            <Link
              key={it.href}
              href={it.href}
              aria-current={active ? "page" : undefined}
              // The active mark is a rule against the edge, not a filled block. A solid accent panel in a
              // navigation rail is the single loudest thing on screen and pulls the eye away from the data.
              className={`relative flex w-full flex-col items-center gap-1 py-2.5 text-[10px] font-medium tracking-wide transition ${
                active
                  ? "text-rail-fg-strong before:absolute before:top-1 before:bottom-1 before:left-0 before:w-0.5 before:rounded-r before:bg-accent"
                  : "text-rail-fg hover:text-rail-fg-strong"
              }`}
            >
              {it.glyph}
              {it.label}
            </Link>
          );
        })}
      </nav>
      <div className="flex min-w-0 grow flex-col">{children}</div>
    </div>
  );
}

/** The page header. One band, always the same shape: what you are looking at, then who you are. */
export function PageHeader({
  title,
  meta,
  badge,
  actions,
}: {
  title: React.ReactNode;
  meta?: React.ReactNode;
  badge?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <header className="sticky top-0 z-10 border-b border-border bg-surface bg-[image:var(--surface-wash)] px-4 py-3 sm:px-5">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="truncate font-mono text-xl font-semibold tracking-tight text-fg">{title}</h1>
            {badge}
          </div>
          {meta && <div className="mt-1 text-sm leading-snug text-fg-muted">{meta}</div>}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {actions}
          <SessionBadge />
        </div>
      </div>
    </header>
  );
}
