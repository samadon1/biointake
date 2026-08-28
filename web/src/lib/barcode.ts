"use client";

/* Reading the barcode off a tube.
 *
 * A receiving bench has a hand scanner, and a hand scanner is just a keyboard; it types the
 * identifier and presses Enter, which the scan box already handles. This is for the other two
 * cases: a lab without one, and a technician who photographs a rack rather than picking up sixty
 * tubes twice.
 *
 * The decoder is a WebAssembly build of ZXing, loaded the first time it is needed and not before.
 * Someone who never opens the camera never downloads it.
 */

import type { ReadInputBarcodeFormat, ReadResult } from "zxing-wasm/reader";

type Reader = (input: Blob | ImageData) => Promise<ReadResult[]>;
let reader: Promise<Reader> | null = null;

function load(): Promise<Reader> {
  reader ??= import("zxing-wasm/reader").then((m) => {
    // The formats a specimen label actually carries. Narrowing it is not premature: every extra
    // symbology is another way to read something off a neighbouring label by mistake.
    const formats: ReadInputBarcodeFormat[] = ["Code128", "Code39", "DataMatrix", "QRCode"];
    return (input: Blob | ImageData) => m.readBarcodes(input, { formats, tryHarder: true });
  });
  return reader;
}

/** Every identifier in one image, in reading order, deduplicated. */
export async function decode(input: Blob | ImageData): Promise<string[]> {
  const results = await (await load())(input);
  const seen = new Set<string>();
  for (const r of results) {
    const text = r.text?.trim();
    if (text) seen.add(text);
  }
  return [...seen];
}

/** Whether this browser can open a camera at all, so the console can offer it or say why not. */
export function cameraAvailable(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}
