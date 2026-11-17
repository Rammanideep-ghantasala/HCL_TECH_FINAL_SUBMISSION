import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * The forbidden strings are assembled from pieces so that this file does not
 * contain them and can be scanned like every other source file.
 */
const FORBIDDEN: Array<{ name: string; pattern: RegExp }> = [
  { name: "raw HTML injection prop", pattern: fromParts("dangerously", "Set", "Inner", "HTML") },
  { name: "inline style attribute", pattern: fromParts("style=", "\\{", "\\{") },
  { name: "element HTML assignment", pattern: fromParts("(inner|outer)", "HTML") },
  { name: "document write", pattern: fromParts("document", "\\.write") },
  { name: "dynamic code evaluation", pattern: fromParts("\\bev", "al\\s*\\(") },
];

function fromParts(...parts: string[]): RegExp {
  return new RegExp(parts.join(""));
}

function listFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? listFiles(path) : [path];
  });
}

describe("source safety scan", () => {
  const files = listFiles(SRC_DIR).filter((path) => /\.(ts|tsx|css|html)$/.test(path));

  it("finds source files to scan", () => {
    expect(files.length).toBeGreaterThan(10);
  });

  it.each(FORBIDDEN)("contains no $name anywhere in src", ({ pattern }) => {
    const offenders = files
      .filter((path) => pattern.test(readFileSync(path, "utf8")))
      .map((path) => relative(SRC_DIR, path));
    expect(offenders).toEqual([]);
  });
});
