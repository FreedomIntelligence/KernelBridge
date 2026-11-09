/** Pi owns continuation; a successful Host checkpoint is only permission to ask again. */
export function createControlledContinuation(controller) {
  let pending = false;
  let disposed = false;

  const extension = pi => {
    pi.on('agent_before_settle', event => {
      const requested = pending;
      pending = false;
      if (disposed || controller.failure || controller.stopped || !requested) return;
      if (event.outcome !== 'completed') return;
      if (event.context.canContinue) return { continue: true };
      // Pi requires non-assistant context at this boundary. This fixed adapter
      // instruction joins the exact HarnessInput checked by managed admission;
      // it neither changes the frozen Task nor bypasses prepare_turn.
      return { continue: true, entries: [{ type: 'custom_message',
        customType: 'kernelcat-checkpoint-continuation', display: false,
        content: 'Continue the original frozen task from its acknowledged checkpoint. Host admission remains required.' }] };
    });
    pi.on('session_shutdown', () => {
      disposed = true;
      pending = false;
    });
  };

  async function finish(message, checkpoint) {
    pending = false;
    if (disposed) return { action: 'end' };
    try {
      const decision = await controller.finish(message, checkpoint);
      pending = !disposed && decision.action === 'continue';
    } catch (error) {
      controller.fail(error);
    }
    // End the low-level tool loop as well. Only the public settlement boundary
    // above may request the next model turn, after all checkpoint work finishes.
    return { action: 'end' };
  }

  return { extension, finish };
}
