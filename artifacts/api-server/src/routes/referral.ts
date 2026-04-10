import { Router } from "express";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { join } from "path";
import { randomUUID } from "crypto";
import type { Request, Response } from "express";

const router = Router();

const DATA_DIR = process.env["BOT_DATA_DIR"] ?? "/home/runner/bot_data";
const IP_FILE  = join(DATA_DIR, "referral_ips.json");

interface TokenEntry {
  ip: string;
  referrer_id: string;
  created_at: string;
  claimed: boolean;
}

interface IpData {
  tokens: Record<string, TokenEntry>;
  used_ips: string[];
}

function loadIpData(): IpData {
  try {
    if (existsSync(IP_FILE)) {
      return JSON.parse(readFileSync(IP_FILE, "utf-8")) as IpData;
    }
  } catch {}
  return { tokens: {}, used_ips: [] };
}

function saveIpData(data: IpData): void {
  try {
    mkdirSync(DATA_DIR, { recursive: true });
    writeFileSync(IP_FILE, JSON.stringify(data, null, 2));
  } catch (e) {
    console.error("Failed to save IP data:", e);
  }
}

function getClientIp(req: Request): string {
  const xff = req.headers["x-forwarded-for"];
  const raw = Array.isArray(xff) ? xff[0] : xff;
  return (
    raw?.split(",")[0]?.trim() ||
    (req.headers["cf-connecting-ip"] as string) ||
    req.socket?.remoteAddress ||
    "unknown"
  );
}

router.get("/:uid", (req: Request, res: Response) => {
  const { uid } = req.params;
  const ip = getClientIp(req);
  const botUsername = process.env["BOT_USERNAME"] ?? "";

  if (!botUsername) {
    res.status(500).send("BOT_USERNAME env var not set");
    return;
  }

  const data = loadIpData();
  const token = randomUUID().replace(/-/g, "");

  data.tokens[token] = {
    ip,
    referrer_id: uid,
    created_at: new Date().toISOString(),
    claimed: false,
  };
  saveIpData(data);

  res.redirect(`https://t.me/${botUsername}?start=ref_${uid}_tk_${token}`);
});

export default router;
