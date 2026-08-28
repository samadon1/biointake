# The BioIntake console, as App Runner runs it.
#
# Built from the repository root so the context matches the API image:
#   docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
#     --output type=docker -f deploy/web/Dockerfile -t biointake-web .

# Pulled from AWS's public mirror of Docker Hub rather than Docker Hub itself: an anonymous pull
# from a CodeBuild worker shares an IP with everyone else's and gets 429 Too Many Requests.
FROM public.ecr.aws/docker/library/node:22-slim AS deps
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci

FROM public.ecr.aws/docker/library/node:22-slim AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web ./
# NEXT_PUBLIC_* is inlined at build time, not read at run time, so the console has to be built
# knowing where its API lives. A rebuild is the cost of pointing it somewhere else.
ARG NEXT_PUBLIC_API_BASE=""
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM public.ecr.aws/docker/library/node:22-slim AS runtime
RUN useradd --create-home --uid 10001 biointake
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=8000 HOSTNAME=0.0.0.0
# `standalone` puts the server and its reachable dependencies here; static and public are served
# from disk alongside it.
COPY --from=build --chown=biointake:biointake /app/.next/standalone ./
COPY --from=build --chown=biointake:biointake /app/.next/static ./.next/static
COPY --from=build --chown=biointake:biointake /app/public ./public
USER biointake
EXPOSE 8000
# node:22-slim carries ENTRYPOINT ["docker-entrypoint.sh"], so a CMD alone becomes
# `docker-entrypoint.sh node server.js`. App Runner pulls the resulting image, declines to run it,
# and writes no application log at all. Clearing the entrypoint makes the command exec directly.
ENTRYPOINT []
# HOSTNAME is set in the command rather than as ENV because the platform injects its own, the
# container id, and a runtime variable beats an image one. Next's standalone server binds to
# whatever HOSTNAME says, so it would try to bind to a name that does not resolve, exit, and leave
# App Runner reporting a failed deployment with no application log to explain it.
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 exec node server.js"]
