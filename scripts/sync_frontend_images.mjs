import { mkdir, readdir, rm, copyFile } from "node:fs/promises";
import { resolve, join } from "node:path";
import { statSync } from "node:fs";

const repoRoot = process.cwd();
const sourceRoot = resolve(repoRoot, "frontend/app/assets/images");
const targetRoot = resolve(repoRoot, "tile_server/static/images");

function exists(path) {
  try {
    statSync(path);
    return true;
  } catch {
    return false;
  }
}

async function syncDir(sourceDir, targetDir) {
  await mkdir(targetDir, { recursive: true });

  const sourceEntries = await readdir(sourceDir, { withFileTypes: true });
  const targetEntries = await readdir(targetDir, { withFileTypes: true });

  const sourceNames = new Set(sourceEntries.map((entry) => entry.name));

  for (const entry of sourceEntries) {
    const sourcePath = join(sourceDir, entry.name);
    const targetPath = join(targetDir, entry.name);

    if (entry.isDirectory()) {
      await syncDir(sourcePath, targetPath);
      continue;
    }

    if (entry.isFile()) {
      await mkdir(targetDir, { recursive: true });
      await copyFile(sourcePath, targetPath);
    }
  }

  for (const entry of targetEntries) {
    if (sourceNames.has(entry.name)) continue;
    await rm(join(targetDir, entry.name), { recursive: true, force: true });
  }
}

async function main() {
  if (!exists(sourceRoot)) {
    console.log("[sync:images] Source folder not found; skipping image sync.");
    console.log("[sync:images] Expected source: frontend/app/assets/images");
    process.exit(0);
  }

  await syncDir(sourceRoot, targetRoot);
  console.log(
    "[sync:images] Synced frontend/app/assets/images -> tile_server/static/images",
  );
}

main().catch((error) => {
  console.error("[sync:images] Failed:", error);
  process.exit(1);
});
