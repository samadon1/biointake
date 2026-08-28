This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

## BioIntake web app

Four surfaces over the control API:

| Route | Who | What |
|---|---|---|
| `/` | coordinator | shipment queue; "Load demonstration shipment" resets, loads and runs the agent |
| `/cases/[id]` | coordinator | evidence matrix (7 checks × 12 samples, `~` = provisional), blockers, demo outbox, activity timeline (domain effects, optionally tool calls), final report |
| `/portal/[requestId]?token=…` | sender | the secure link from the evidence request: message + file upload |
| `/cases/[id]/decide/[interruptId]` | coordinator/PI | decision card; a refused option re-raises a fresh interrupt so the card stays answerable |

```bash
npm run dev            # :3000, expects the control API on :8000 (make api-dev)
```
`NEXT_PUBLIC_API_BASE` points at the control API (default `http://127.0.0.1:8000`).
The persona selector only picks *who you are signing in as*; the server assigns the role and refuses
anything that role may not do.
