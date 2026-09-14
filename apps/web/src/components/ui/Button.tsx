"use client";

import React from "react";

type ButtonVariant = "primary" | "secondary" | "outline" | "ghost" | "danger";
type ButtonSize = "sm" | "md" | "lg";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: React.ReactNode;
}

const variantClasses: Record<ButtonVariant, string> = {
  primary: "bg-fc-primary text-white hover:bg-fc-primary-deep active:bg-fc-primary-deep",
  secondary: "bg-fc-surface text-fc-ink border border-fc-ink hover:bg-fc-bg",
  outline: "bg-transparent border border-fc-line text-fc-ink hover:bg-fc-bg",
  ghost: "bg-transparent text-fc-ink-soft hover:bg-fc-bg",
  danger: "bg-fc-danger text-white hover:opacity-90 active:opacity-80",
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: "px-3 py-1.5 text-sm min-h-[40px]",
  md: "px-4 py-2 text-base min-h-touch",
  lg: "px-6 py-3 text-lg min-h-[56px]",
};

export default function Button({
  variant = "primary",
  size = "md",
  className = "",
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      className={`inline-flex items-center justify-center rounded-fc font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-fc-primary focus:ring-offset-2 focus:ring-offset-fc-bg disabled:opacity-50 disabled:cursor-not-allowed ${variantClasses[variant]} ${sizeClasses[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
