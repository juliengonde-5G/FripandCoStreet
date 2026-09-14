"use client";

import React from "react";

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  icon?: React.ReactNode;
}

export default function Input({
  label,
  error,
  icon,
  className = "",
  id,
  ...props
}: InputProps) {
  const inputId = id || label?.toLowerCase().replace(/\s+/g, "-");

  return (
    <div className="w-full">
      {label && (
        <label
          htmlFor={inputId}
          className="block text-[11px] uppercase tracking-[0.12em] font-medium text-fc-ink-soft mb-1.5"
        >
          {label}
        </label>
      )}
      <div className="relative">
        {icon && (
          <div className="absolute left-3 top-1/2 -translate-y-1/2 text-fc-ink-soft">
            {icon}
          </div>
        )}
        <input
          id={inputId}
          className={`w-full min-h-touch px-4 py-2.5 rounded-fc border bg-fc-surface text-fc-ink placeholder-fc-ink-soft transition-colors focus:outline-none focus:ring-2 focus:ring-fc-primary focus:border-fc-primary ${
            icon ? "pl-10" : ""
          } ${
            error
              ? "border-fc-danger focus:ring-fc-danger focus:border-fc-danger"
              : "border-fc-line"
          } ${className}`}
          {...props}
        />
      </div>
      {error && <p className="mt-1 text-sm text-fc-danger">{error}</p>}
    </div>
  );
}
