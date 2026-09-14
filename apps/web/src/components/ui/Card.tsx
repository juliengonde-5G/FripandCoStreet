import React from "react";

interface CardProps {
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
  action?: React.ReactNode;
}

export default function Card({ title, subtitle, children, className = "", action }: CardProps) {
  const header = (title || subtitle || action) && (
    <div className="flex items-start justify-between gap-4 mb-4">
      <div className="min-w-0">
        {title && <h3 className="text-lg font-semibold leading-tight text-fc-ink">{title}</h3>}
        {subtitle && <p className="text-sm mt-0.5 text-fc-ink-soft">{subtitle}</p>}
      </div>
      {action && <div className="flex-shrink-0">{action}</div>}
    </div>
  );

  return (
    <div className={`rounded-fc-lg p-6 bg-fc-surface border border-fc-line ${className}`}>
      {header}
      {children}
    </div>
  );
}
