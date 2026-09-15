import {
  Action,
  ActionPanel,
  Detail,
  Icon,
  Keyboard,
  Toast,
  environment,
  getPreferenceValues,
  showToast,
} from "@raycast/api";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { useEffect, useMemo, useState } from "react";

interface Preferences {
  backendPath?: string;
}

interface CommandState {
  report?: string;
  error?: string;
}

function backendCandidates(preference?: string): string[] {
  const repository = dirname(environment.assetsPath);
  return [
    preference?.trim() ?? "",
    join(repository, "../.venv/bin/ai-usage"),
    join(process.cwd(), "../.venv/bin/ai-usage"),
    join(homedir(), ".local/bin/ai-usage"),
    "/opt/homebrew/bin/ai-usage",
    "/usr/local/bin/ai-usage",
  ].filter(Boolean);
}

function findBackend(preference?: string): string {
  const backend = backendCandidates(preference).find(existsSync);
  if (!backend) {
    throw new Error(
      "Backend niet gevonden. Voer `uv tool install .` uit in de repository of stel Backend Path in.",
    );
  }
  return backend;
}

function runBackend(backend: string): Promise<string> {
  const path = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    join(homedir(), ".local/bin"),
    process.env.PATH,
  ]
    .filter(Boolean)
    .join(":");
  return new Promise((resolve, reject) => {
    execFile(
      backend,
      [],
      {
        encoding: "utf8",
        env: { ...process.env, PATH: path },
        maxBuffer: 2 * 1024 * 1024,
        timeout: 120_000,
      },
      (error, stdout) => {
        const output = stdout.trim();
        if (error) {
          reject(
            new Error(
              output || "De AI Usage-backend kon niet worden uitgevoerd.",
            ),
          );
          return;
        }
        if (!output) {
          reject(new Error("De AI Usage-backend gaf geen rapport terug."));
          return;
        }
        resolve(output);
      },
    );
  });
}

function reportMarkdown(report: string): string {
  return `\`\`\`text\n${report.replaceAll("```", "ʼʼʼ")}\n\`\`\``;
}

export default function Command() {
  const preferences = getPreferenceValues<Preferences>();
  const [state, setState] = useState<CommandState>({});
  const [reload, setReload] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const loading = !state.report && !state.error;

  useEffect(() => {
    let active = true;
    const started = Date.now();
    setState({});
    setElapsed(0);
    const timer = setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );

    async function load() {
      try {
        const report = await runBackend(findBackend(preferences.backendPath));
        if (active) setState({ report });
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Onbekende fout";
        if (active) {
          setState({ error: message });
          await showToast({
            style: Toast.Style.Failure,
            title: "AI Usage mislukt",
            message,
          });
        }
      } finally {
        clearInterval(timer);
      }
    }

    void load();
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [preferences.backendPath, reload]);

  const markdown = useMemo(() => {
    if (state.report) return reportMarkdown(state.report);
    if (state.error)
      return `# AI Usage kon niet worden geladen\n\n${state.error}`;
    return `# AI Usage\n\nGegevens verzamelen… ${elapsed}s`;
  }, [elapsed, state.error, state.report]);

  return (
    <Detail
      isLoading={loading}
      markdown={markdown}
      actions={
        <ActionPanel>
          {state.report ? (
            <Action.CopyToClipboard
              title="Copy Report"
              content={state.report}
              shortcut={Keyboard.Shortcut.Common.Copy}
            />
          ) : null}
          <Action
            title="Refresh"
            icon={Icon.ArrowClockwise}
            shortcut={Keyboard.Shortcut.Common.Refresh}
            onAction={() => setReload((value) => value + 1)}
          />
        </ActionPanel>
      }
    />
  );
}
