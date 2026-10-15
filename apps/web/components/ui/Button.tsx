"use client";
import { Loader2, type LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

/**
 * The single button for OpenCortyx. Variant is chosen by what the action MEANS
 * (its category), not by page — so the same kind of action looks the same
 * everywhere. Atlan/DataHub-style: one size scale, one weight, brand-forward CTA.
 *
 *   primary   → the main CTA (Create / Save / Run) — brand emerald, one per screen
 *   secondary → neutral (Refresh / Cancel / Back)  — white + stone border
 *   ghost     → low-emphasis inline action          — transparent, hover tint
 *   danger    → destructive (Delete / Purge / Reset)— red
 */
export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm focus-visible:ring-primary/40",
  secondary:
    "bg-white text-stone-700 border border-stone-200 hover:bg-stone-50 shadow-sm focus-visible:ring-stone-300",
  ghost:
    "bg-transparent text-stone-600 hover:bg-stone-100 focus-visible:ring-stone-300",
  danger:
    "bg-red-600 text-white hover:bg-red-500 shadow-sm focus-visible:ring-red-400",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "px-3 py-1.5 text-sm gap-1.5",
  md: "px-3.5 py-2 text-sm gap-2",
};

const ICON_SIZE: Record<ButtonSize, number> = { sm: 15, md: 16 };

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: LucideIcon;          // leading icon
  iconRight?: LucideIcon;     // trailing icon
  busy?: boolean;             // show a spinner + disable
  children?: ReactNode;
}

export default function Button({
  variant = "secondary",
  size = "md",
  icon: Icon,
  iconRight: IconRight,
  busy = false,
  disabled,
  className = "",
  children,
  ...rest
}: ButtonProps) {
  const sz = ICON_SIZE[size];
  return (
    <button
      disabled={disabled || busy}
      className={`inline-flex items-center justify-center rounded-lg font-semibold transition-colors
        outline-none focus-visible:ring-2 disabled:opacity-50 disabled:pointer-events-none
        ${SIZES[size]} ${VARIANTS[variant]} ${className}`}
      {...rest}
    >
      {busy ? <Loader2 size={sz} className="animate-spin" /> : Icon ? <Icon size={sz} /> : null}
      {children}
      {IconRight && !busy && <IconRight size={sz} />}
    </button>
  );
}
