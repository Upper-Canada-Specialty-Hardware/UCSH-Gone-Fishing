/**
 * "Good morning", "Good afternoon" or "Good evening" by the clock of the
 * device showing the page, not the server's: it runs in the browser, so it
 * follows whatever time zone that computer or phone is set to.
 *
 * @param now - The moment to greet for; defaults to now on this device.
 * @returns The greeting, without a name.
 */
export function greeting(now: Date = new Date()): string {
  const h = now.getHours();                                  // local hour, 0 to 23
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}
