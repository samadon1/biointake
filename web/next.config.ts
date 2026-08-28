import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The console ships as a container. `standalone` emits a server plus only the dependencies it
  // actually reached for, so the runtime image carries a tenth of node_modules rather than all of it.
  output: "standalone",
};

export default nextConfig;
