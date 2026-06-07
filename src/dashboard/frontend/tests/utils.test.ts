/**
 * Bonus: theme & format helper tests.
 */

import { describe, expect, it } from "vitest";
import { getStatusTokens } from "../src/theme";
import {
  formatBytes,
  formatCount,
  formatCurrency,
  formatDuration,
  formatPercent,
  formatRelative,
  formatTime,
} from "../src/utils/format";
import { detectLanguage } from "../src/utils/language";

describe("theme tokens", () => {
  it("returns a token set for known statuses", () => {
    const tokens = getStatusTokens("running");
    expect(tokens.label).toBe("Running");
    expect(tokens.dot).toBeTruthy();
  });

  it("falls back to idle for unknown statuses", () => {
    const tokens = getStatusTokens("not-a-status");
    expect(tokens.label).toBe("Idle");
  });
});

describe("format helpers", () => {
  it("formats bytes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1024)).toBe("1.00 KB");
    expect(formatBytes(2_000_000)).toContain("MB");
  });
  it("formats counts", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(1234)).toBe("1.2k");
    expect(formatCount(2_500_000)).toContain("M");
  });
  it("formats percents", () => {
    expect(formatPercent(0.5, 1)).toBe("50.0%");
    expect(formatPercent(1)).toBe("100.0%");
  });
  it("formats durations", () => {
    expect(formatDuration(45)).toBe("45.0s");
    expect(formatDuration(125)).toBe("2m 05s");
  });
  it("formats currency", () => {
    expect(formatCurrency(0.0009)).toContain("$");
    expect(formatCurrency(1.234)).toBe("$1.23");
  });
  it("formats time as UTC HH:MM:SS", () => {
    expect(formatTime("2026-06-06T10:00:00Z")).toBe("10:00:00 UTC");
  });
  it("formats relative strings", () => {
    const fiveSecsAgo = new Date(Date.now() - 5_000).toISOString();
    expect(formatRelative(fiveSecsAgo)).toMatch(/s ago/);
  });
});

describe("detectLanguage", () => {
  it.each([
    ["src/main.ts", "typescript"],
    ["README.md", "markdown"],
    ["foo.py", "python"],
    ["Dockerfile", "dockerfile"],
    ["unknown.xyz", "plaintext"],
  ])("maps %s -> %s", (path, expected) => {
    expect(detectLanguage(path)).toBe(expected);
  });
});
