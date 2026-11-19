import Image from "next/image";

export function BrandLogo() {
  return (
    <span className="pb-logo">
      <span className="pb-logo-shell" aria-hidden="true">
        <Image src="/images/brand/parkingbreaker-pb-shell.webp" alt="" width={64} height={64} sizes="64px" />
      </span>
      <span className="pb-logo-name">Parking<span>Breaker</span></span>
    </span>
  );
}
