/* The label sheet the example shipment ships with must actually decode.
 *
 * It did not, twice, for reasons that had nothing to do with the decoder: the symbols were about a
 * pixel per module at first, and the last two labels sat inside the sheet's own quiet zone. Neither
 * would have shown up until someone pointed a camera at a screen and nothing happened.
 *
 *   npm test
 *
 * The fixture is a 2x render of example-shipment/7-tube-labels.svg, committed so the test needs no
 * browser to run.
 */
import { readBarcodes } from "zxing-wasm/reader";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";

const RENDER = new URL("./fixtures/labels.png", import.meta.url);

test("every tube label on the sheet decodes", async () => {
  const found = await readBarcodes(new Blob([readFileSync(RENDER)]), {
    formats: ["Code128"],
    tryHarder: true,
    maxNumberOfSymbols: 60,
  });
  const read = new Set(found.map((r) => r.text?.trim()).filter(Boolean));
  const expected = Array.from({ length: 12 }, (_, i) => `NS042-0002${String(i + 1).padStart(2, "0")}`);
  assert.deepEqual(
    expected.filter((e) => !read.has(e)),
    [],
    "a label on the sheet could not be read",
  );
});
