"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { cameraAvailable, decode } from "@/lib/barcode";
import { Button } from "@/components/ui";

/* Reading a label with a camera instead of a hand scanner.
 *
 * Two ways in, because a bench has two situations. The camera is for scanning tubes one at a time
 * with whatever device is to hand. The photograph is for a rack that has already been unpacked and
 * photographed, or a label sheet, one image, every identifier on it.
 *
 * A decoded value is submitted exactly as a typed one is. This is an input method, not a second
 * path into the case: everything downstream, including the near-match that catches a letter O read
 * as a digit zero, is unchanged.
 */
export function BarcodeScanner({
  onDecode,
  label = "Scan with the camera",
}: {
  onDecode: (values: string[]) => void;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [photo, setPhoto] = useState<{ url: string; name: string; values: string[] } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  // The last thing read, so holding a tube still does not submit it forty times a second.
  const lastRef = useRef<{ value: string; at: number }>({ value: "", at: 0 });

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setOpen(false);
  }, []);

  useEffect(() => stop, [stop]);
  // An object URL outlives the component unless it is revoked, and the bench mounts one scanner per
  // mode switch.
  useEffect(() => () => { if (photo) URL.revokeObjectURL(photo.url); }, [photo]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    let timer: number | undefined;

    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
        });
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        const video = videoRef.current;
        if (!video) return;
        video.srcObject = stream;
        await video.play();
        setStatus("Point the camera at a label.");

        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d", { willReadFrequently: true });

        const tick = async () => {
          if (cancelled || !ctx || !video.videoWidth) {
            timer = window.setTimeout(tick, 200);
            return;
          }
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0);
          try {
            const found = await decode(ctx.getImageData(0, 0, canvas.width, canvas.height));
            const now = Date.now();
            for (const value of found) {
              // Two seconds is long enough to move to the next tube and short enough that scanning
              // the same one twice on purpose still registers.
              if (lastRef.current.value === value && now - lastRef.current.at < 2000) continue;
              lastRef.current = { value, at: now };
              setStatus(`Read ${value}`);
              onDecode([value]);
            }
          } catch {
            /* a frame that will not decode is the normal case, not an error */
          }
          if (!cancelled) timer = window.setTimeout(tick, 250);
        };
        void tick();
      } catch {
        setStatus("This device would not give the page a camera. Type the identifier instead.");
        setOpen(false);
      }
    })();

    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [open, onDecode]);

  async function onFile(file: File | null) {
    if (!file) return;
    setStatus("Reading…");
    // Show the photograph that was read, next to what was read out of it. A scanner that silently
    // fills a field leaves the person with nothing to check it against; a technician holding a
    // blurred photo of the wrong rack should be able to see that is what happened.
    setPhoto((prev) => {
      if (prev) URL.revokeObjectURL(prev.url);
      return { url: URL.createObjectURL(file), name: file.name, values: [] };
    });
    try {
      const found = await decode(file);
      if (found.length === 0) {
        setStatus("No barcode in that image. A closer, straighter photograph usually reads.");
        setPhoto((prev) => (prev ? { ...prev, values: [] } : prev));
        return;
      }
      setStatus(`Read ${found.length} label${found.length === 1 ? "" : "s"}.`);
      setPhoto((prev) => (prev ? { ...prev, values: found } : prev));
      onDecode(found);
    } catch {
      setStatus("That image could not be read.");
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        {cameraAvailable() ? (
          <Button variant="ghost" onClick={() => (open ? stop() : setOpen(true))}>
            {open ? "Stop the camera" : label}
          </Button>
        ) : null}
        <label className="text-sm text-fg-muted">
          <span className="cursor-pointer underline">or read a photograph of the labels</span>
          <input
            type="file"
            accept="image/*"
            className="sr-only"
            onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
          />
        </label>
      </div>
      {open ? (
        <video
          ref={videoRef}
          muted
          playsInline
          className="w-full max-w-md rounded border border-border bg-black"
        />
      ) : null}
      {photo ? (
        <div className="flex items-start gap-3 rounded border border-border bg-surface-2 p-2">
          {/* eslint-disable-next-line @next/next/no-img-element -- an object URL for a file the
              user just chose; there is nothing for the image optimiser to do with it. */}
          <img src={photo.url} alt={`the photograph read: ${photo.name}`} className="h-20 w-auto rounded border border-border" />
          <div className="min-w-0 text-[13px]">
            <div className="font-mono text-fg-muted">{photo.name}</div>
            {photo.values.length > 0 ? (
              <ul className="mt-1 space-y-0.5 font-mono text-fg">
                {photo.values.slice(0, 6).map((v) => (
                  <li key={v}>{v}</li>
                ))}
                {photo.values.length > 6 ? <li className="text-fg-muted">and {photo.values.length - 6} more</li> : null}
              </ul>
            ) : (
              <div className="mt-1 text-fg-muted">nothing read from this image</div>
            )}
          </div>
        </div>
      ) : null}
      {status ? <p className="text-[13px] text-fg-muted">{status}</p> : null}
    </div>
  );
}
