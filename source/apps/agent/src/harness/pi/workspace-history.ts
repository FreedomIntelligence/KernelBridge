import { readFile, realpath, writeFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { isAbsolute, join, relative, sep } from 'node:path';
import type { AgentSession, DefaultResourceLoader, SettingsManager } from '@earendil-works/pi-coding-agent';

type ResourceOptions = ConstructorParameters<typeof DefaultResourceLoader>[0];

export interface WorkspaceHistoryOptions {
  readonly workspace: string;
  readonly agentDir: string;
  readonly storageDir: string;
  readonly packageDir: string;
}

export interface PreparedWorkspaceHistory {
  readonly packageVersion: '0.5.2';
  readonly extensionPath: string;
  readonly agentDir: string;
  readonly storageDir: string;
  readonly settingsPath: string;
}

function requireOutside(workspace: string, path: string): void {
  const relation = relative(workspace, path);
  if (relation === '' || (!relation.startsWith('..' + sep) && relation !== '..' && !isAbsolute(relation))) {
    throw new Error(`Workspace history path must be outside workspace: ${path}`);
  }
}

/** Prepare only Controller-owned existing directories; never install or discover a plugin. */
export async function prepareWorkspaceHistory(options: WorkspaceHistoryOptions): Promise<PreparedWorkspaceHistory> {
  for (const path of Object.values(options)) {
    if (!isAbsolute(path)) throw new Error('Workspace history paths must be absolute');
  }
  const [workspace, agentDir, storageDir, packageDir] = await Promise.all([
    realpath(options.workspace), realpath(options.agentDir), realpath(options.storageDir), realpath(options.packageDir),
  ]);
  for (const path of [agentDir, storageDir, packageDir]) requireOutside(workspace, path);
  const manifest = JSON.parse(await readFile(join(packageDir, 'package.json'), 'utf8'));
  if (manifest.name !== 'pi-workspace-history' || manifest.version !== '0.5.2') {
    throw new Error('Workspace history requires the explicitly pinned pi-workspace-history 0.5.2');
  }
  const extensionPath = await realpath(join(packageDir, '.pi/extensions/workspace-history.ts'));
  requireOutside(workspace, extensionPath);
  const settingsPath = join(agentDir, 'settings.json');
  const contents = JSON.stringify({ workspaceHistory: { enabled: true, storageDir,
    maxSessionsPerWorkspace: Number.MAX_SAFE_INTEGER, maxWorkspaces: Number.MAX_SAFE_INTEGER,
  } }, null, 2) + '\n';
  try {
    await writeFile(settingsPath, contents, { flag: 'wx', mode: 0o600 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
    if (await readFile(settingsPath, 'utf8') !== contents) {
      throw new Error(`Refusing to replace existing Controller workspace-history settings: ${settingsPath}`, { cause: error });
    }
  }
  return { packageVersion: '0.5.2', extensionPath, agentDir, storageDir, settingsPath };
}

/** Call for every runtime replacement; the upstream plugin reads this process environment. */
export function workspaceHistoryResources(
  prepared: PreparedWorkspaceHistory, settings: SettingsManager,
): Pick<ResourceOptions, 'additionalExtensionPaths' | 'noExtensions' | 'noSkills' | 'noPromptTemplates' | 'noThemes' | 'noContextFiles'> {
  if (!process.env.PI_CODING_AGENT_DIR || realpathSync(process.env.PI_CODING_AGENT_DIR) !== prepared.agentDir) {
    throw new Error('PI_CODING_AGENT_DIR must identify the controlled workspace-history agent directory');
  }
  if (settings.isProjectTrusted()) throw new Error('Workspace history must not trust project configuration');
  return { additionalExtensionPaths: [prepared.extensionPath], noExtensions: true,
    noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true };
}

/** Query the upstream command without prompting the model; retain its complete diagnostic text. */
export async function observeWorkspaceHistory(session: AgentSession): Promise<readonly string[]> {
  const runner = session.extensionRunner;
  const command = runner.getCommand('history-status');
  if (!command) throw new Error('Pinned workspace-history command was not loaded');
  const context = runner.createCommandContext();
  const messages: string[] = [];
  await command.handler('', { ...context, ui: { ...context.ui,
    notify: message => messages.push(message),
  } });
  if (messages.length === 0) throw new Error('Workspace-history status returned no observation');
  return messages;
}
