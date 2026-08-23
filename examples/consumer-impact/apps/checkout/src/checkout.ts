export function checkout(paymentId: string): string {
  return `checkout:${paymentId}`;
}
