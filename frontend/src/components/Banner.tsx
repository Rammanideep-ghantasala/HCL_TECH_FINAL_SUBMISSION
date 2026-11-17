import type { ReactNode } from "react";

export type BannerTone = "info" | "neutral" | "warning" | "error";

interface BannerProps {
  tone: BannerTone;
  /** Short text tag so the kind of banner never depends on colour alone. */
  tag: string;
  title: string;
  children?: ReactNode;
}

export function Banner({ tone, tag, title, children }: BannerProps) {
  return (
    <div className={`banner banner-${tone}`}>
      <p className="banner-heading">
        <span className="banner-tag">{tag}</span>
        <span className="banner-title">{title}</span>
      </p>
      {children ? <div className="banner-body">{children}</div> : null}
    </div>
  );
}
