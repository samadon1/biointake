"use client";

/* The console is dark, and that is the whole of the theme system.
 *
 * There was a light/dark/system switcher. It went because a person receiving specimens is not
 * choosing a colour scheme, and every preference offered is a preference to be got wrong on the
 * one screen that matters. The sender portal sets `data-theme="light"` on its own root and stays
 * light deliberately: it is opened from an email by someone who does not work at the lab, and the
 * dark operations chrome would read as somebody else's software.
 */

const THEME = "dark";

/** Inlined in <head> so the theme is painted before first render rather than flashing into it. */
export const THEME_BOOTSTRAP =
  `document.documentElement.setAttribute("data-theme","${THEME}");` +
  `document.documentElement.style.colorScheme="${THEME}";`;
