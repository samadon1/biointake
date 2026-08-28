"use client";

/* ---- showing a near-match so a person can actually see it -------------------------------------

   The whole value of catching `BX-4O2` against `BX-402` evaporates if the two are rendered
   identically, and in most monospace faces a capital O and a digit zero are near enough to
   identical at bench distance. So the differing characters are marked, and named in words
   underneath. Naming them is the part that matters: "letter O, not digit zero" is unambiguous in a
   way that any rendering of the glyph itself is not.
--------------------------------------------------------------------------------------------- */

const GLYPH_NAMES: Record<string, string> = {
  O: "letter O", "0": "digit zero",
  I: "letter I", "1": "digit one", L: "letter L",
  S: "letter S", "5": "digit five",
  B: "letter B", "8": "digit eight",
};

function describeGlyph(c: string): string {
  return GLYPH_NAMES[c.toUpperCase()] ?? `"${c}"`;
}

/** The scanned value with every character that differs from the declared one marked. */
export function DiffedValue({ declared, scanned }: { declared: string; scanned: string }) {
  if (declared.length !== scanned.length) return <span className="font-mono">{scanned}</span>;
  return (
    <span className="font-mono">
      {scanned.split("").map((c, i) => (
        <span
          key={i}
          // A tint behind the character, not an inversion of it. Filling the cell with the strong colour and
          // flipping the glyph to the background colour made the one character the reader is here to
          // examine into a dark blob.
          className={
            c !== declared[i]
              ? "rounded-[3px] bg-warn-fg/25 px-0.5 font-bold text-fg underline decoration-warn-fg decoration-2 underline-offset-2"
              : undefined
          }
        >
          {c}
        </span>
      ))}
    </span>
  );
}

/** "position 5: digit zero on the manifest, letter O on the tube" */
export function glyphDifference(
  declared: string,
  scanned: string,
  where: { declared: string; scanned: string } = { declared: "on the manifest", scanned: "on the tube" },
): string | null {
  if (declared.length !== scanned.length) return null;
  const diffs = [];
  for (let i = 0; i < declared.length; i++) {
    if (declared[i] !== scanned[i]) {
      diffs.push(
        `position ${i + 1}: ${describeGlyph(declared[i])} ${where.declared}, ${describeGlyph(scanned[i])} ${where.scanned}`,
      );
    }
  }
  return diffs.length ? diffs.join("; ") : null;
}


/** Two identifiers pulled out of a sentence like:
 *      manifest row 7 reads 'BX-2O7'; label reads 'BX-207'
 *  Requirement descriptions are written by the backend for a human to read, and this is the one place a
 *  site coordinator decides which of the two is correct, so the difference has to be visible in the text
 *  they are reading, not merely present in it. */
const QUOTED = /'([^']{2,40})'/g;

export function NearMatchExplainer({ text }: { text: string }) {
  const quoted = [...text.matchAll(QUOTED)].map((m) => m[1]);
  if (quoted.length !== 2) return <>{text}</>;
  const [a, b] = quoted;
  const difference = glyphDifference(a, b, { declared: "on the manifest", scanned: "on the label" });
  if (!difference) return <>{text}</>;
  return (
    <>
      {text}
      <span className="mt-1 flex flex-wrap items-center gap-2 text-sm">
        <span className="rounded border border-warn-border bg-warn-bg px-1.5 py-0.5 text-warn-fg">
          <DiffedValue declared={b} scanned={a} /> vs <DiffedValue declared={a} scanned={b} />
        </span>
        <span className="text-fg-muted">{difference}</span>
      </span>
    </>
  );
}
