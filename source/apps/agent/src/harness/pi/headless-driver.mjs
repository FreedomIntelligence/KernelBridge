export async function runHeadless(governed, actions) {
  await actions.bind(governed);
  await actions.headless();
}
