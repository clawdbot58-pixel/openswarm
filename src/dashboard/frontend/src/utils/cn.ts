/**
 * `cn` — classname combiner with Tailwind-aware merging.
 *
 * We use `tailwind-merge` to ensure later utilities win (e.g.
 * `cn("p-2", "p-4")` becomes `p-4`) and `clsx` for falsy handling.
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
