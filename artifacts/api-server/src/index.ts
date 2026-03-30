import app from "./app";
import { logger } from "./lib/logger";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");

  if (process.env["NODE_ENV"] === "production") {
    const PING_INTERVAL_MS = 4 * 60 * 1000;
    setInterval(() => {
      fetch(`http://localhost:${port}/api/healthz`)
        .then(() => logger.info("Keep-alive ping sent"))
        .catch((e) => logger.warn({ err: e }, "Keep-alive ping failed"));
    }, PING_INTERVAL_MS);
    logger.info("Keep-alive self-ping enabled (every 4 min)");
  }
});
