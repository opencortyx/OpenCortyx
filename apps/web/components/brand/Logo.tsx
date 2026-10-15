/**
 * LogoMark — the OpenCortyx symbol (v2 mascot, served from /brand).
 */
export function LogoMark({
  size = 24,
  className,
}: {
  size?: number;
  className?: string;
}) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/brand/opencortyx-logo.png"
      alt=""
      aria-hidden="true"
      height={size}
      style={{ height: size, width: "auto" }}
      className={className}
    />
  );
}

/**
 * Logo — symbol + wordmark lockup used in the app shell.
 */
export function Logo({
  showWordmark = true,
  className = "",
}: {
  showWordmark?: boolean;
  className?: string;
}) {
  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <LogoMark size={40} />
      {showWordmark && (
        <span className="text-[1.3rem] leading-none tracking-tight">
          <span className="font-medium text-foreground/80">Open</span>
          <span className="font-semibold text-foreground">Cortyx</span>
        </span>
      )}
    </div>
  );
}
