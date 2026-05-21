import { platform } from "@tauri-apps/plugin-os";

export async function getRevealLabel(): Promise<string> {
  try {
    const os = await platform();
    if (os === "windows") return "Show in Explorer";
    if (os === "macos") return "Reveal in Finder";
    return "Show in Files";
  } catch {
    return "Show in Folder";
  }
}
