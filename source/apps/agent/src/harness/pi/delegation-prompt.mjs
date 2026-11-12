// Capability-specific adaptation; sources and deliberate differences: docs/prompt-migration.md.
export function delegationPrompt(tools) {
  const delegate = tools.find(tool => tool.name === 'delegate');
  if (!delegate) return '';
  const properties = delegate.parameters?.properties ?? {};
  const agents = properties.agent?.enum ?? [];
  // Role names are configurable; their authority comes from the admitted tool
  // catalogue, not from a name that happens to match a built-in role.
  const choices = agents.length ? agents.join(', ') : 'see the delegate tool definition';
  const description = properties.description
    ? 'Supply a short description (3-6 words) that the user sees while each child runs.' : '';
  const limit = properties.max_steps ? 'Leave max_steps unset unless a smaller bound is needed.' : '';
  const background = properties.background
    ? 'Use background=true when you can continue independent work; read and act on its later [子代理结果] message.'
    : 'This delegate interface returns its result through the admitted call; do not invent a background parameter.';
  const status = tools.some(tool => tool.name === 'agents_status')
    ? 'Use agents_status only when a status check changes the next decision; do not poll it in a loop.' : '';
  return `Subagents
delegate launches a real child agent; a request to start agents is an action, not a phrase to search for in the repository. Follow its configured policy and any user/project prohibition.
When proactive use is enabled and the request asks to audit, review or investigate two or more independent components separately, launch one child per component in the same reply (they run concurrently) before reading those components yourself, then merge their reports. No explicit request for an agent is needed. Keep a short lookup or a tightly dependent edit in the main conversation.
Available roles: ${choices}. Follow the configured role descriptions and permissions in the delegate tool definition; never infer permissions from a role name. Use only roles and parameters present in its schema. Give separate children distinct questions. ${description} Explain the division briefly to the user and avoid repeating the delegated search yourself.
Each child starts without the conversation. Include the objective, why it matters, relevant findings and rejected approaches, project-relative paths, applicable user/project constraints, and the expected answer. Ask an investigative question rather than prescribing a long sequence of reads. Request a short report with file references, supported findings and unresolved questions.
Provide relevant diagnostic excerpts and source identities in the child request: a parent receipt path is not evidence that the child can access it. Distinguish observed results from hypotheses and never present an unverified prior attempt as fact. Children may use only their declared tools and workspace; no child publishes, uses devices or spawns children. Review any returned diff before applying it. Implement tightly coupled edits yourself. ${limit}
Delegate calls issued together in one reply run concurrently; issue independent investigations in the same reply instead of one after another. Each returns through its own delegate result. ${background} ${status} There is no inherited-history child fork; /fork creates a user conversation branch and is not a child launch.
Read the returned findings, distinguish evidence from suggestions, and combine them into the answer or implementation. A partial result or failure is not a completed investigation; continue its unresolved portion yourself when practical. Do not repeat an unchanged failed delegation. Never turn child advice into a claim that tests or device measurements ran.`;
}
