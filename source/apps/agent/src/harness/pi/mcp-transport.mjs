/** Local MCP server transport. The native Pi client owns protocol discovery and validation. */
export class GatewayMcpTransport {
  constructor(specifications, invoke) {
    this.specifications = new Map(specifications.map(specification => [specification.name, specification]));
    this.invoke = invoke;
    this.listeners = { message: new Set(), error: new Set(), close: new Set() };
    this.started = false;
    this.closed = false;
  }

  onMessage(listener) { return this.subscribe('message', listener); }
  onError(listener) { return this.subscribe('error', listener); }
  onClose(listener) { return this.subscribe('close', listener); }

  subscribe(event, listener) {
    this.listeners[event].add(listener);
    return () => this.listeners[event].delete(listener);
  }

  async start() {
    if (this.closed) throw new Error('The Gateway MCP transport is closed');
    this.started = true;
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    for (const listener of this.listeners.close) listener();
    for (const listeners of Object.values(this.listeners)) listeners.clear();
  }

  async send(request) {
    if (!this.started || this.closed) throw new Error('The Gateway MCP transport is not open');
    if (!Object.hasOwn(request, 'id')) return;
    let response;
    try {
      response = { jsonrpc: '2.0', id: request.id, result: await this.handle(request) };
    } catch (error) {
      response = { jsonrpc: '2.0', id: request.id, error: { code: -32603, message: String(error) } };
    }
    if (this.closed) return;
    for (const listener of this.listeners.message) listener(structuredClone(response));
  }

  async handle(request) {
    switch (request.method) {
      case 'initialize':
        return { protocolVersion: request.params.protocolVersion, capabilities: { tools: {} },
          serverInfo: { name: 'KernelCat Gateway', version: '1' },
          instructions: 'Finite tools admitted by the KernelCat Gateway. Discovery grants no permission.' };
      case 'ping':
        return {};
      case 'tools/list':
        return { tools: [...this.specifications.values()].map(specification => ({
          name: specification.name, description: specification.description,
          inputSchema: specification.parameters,
        })) };
      case 'tools/call': {
        const { name, arguments: arguments_ } = request.params;
        if (!this.specifications.has(name)) throw new Error(`Unknown Gateway MCP tool: ${name}`);
        return await this.invoke(name, arguments_);
      }
      default:
        throw new Error(`Unsupported Gateway MCP method: ${request.method}`);
    }
  }
}
