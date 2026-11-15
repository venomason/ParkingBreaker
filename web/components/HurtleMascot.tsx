import React from "react";

export function PBShieldIcon({ size = 36, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      className={className}
      aria-hidden="true"
    >
      <path
        d="M32 4L8 14V31C8 46.5 18.2 58.6 32 62C45.8 58.6 56 46.5 56 31V14L32 4Z"
        fill="#0B1D33"
        stroke="#E5A93C"
        strokeWidth="4"
        strokeLinejoin="round"
      />
      <path
        d="M32 9L13 17.2V31C13 43.4 21.1 53.3 32 56.4C42.9 53.3 51 43.4 51 31V17.2L32 9Z"
        fill="#163A5F"
      />
      <path
        d="M21 33L28.5 40.5L44 23"
        stroke="#4E9F3D"
        strokeWidth="6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="32" cy="16" r="2.5" fill="#E5A93C" />
    </svg>
  );
}

export function HurtleAvatarIcon({ size = 44 }: { size?: number }) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/images/hurtle/17-support-avatar.png"
      alt=""
      width={size}
      height={size}
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        objectFit: "cover",
        borderRadius: "50%",
        display: "block",
      }}
    />
  );
}

const POSE_SRC: Record<"welcoming" | "guiding" | "waiting", string> = {
  welcoming: "/images/hurtle/15-welcoming-pose.png",
  guiding: "/images/hurtle/16-thumbs-up-pose.png",
  waiting: "/images/hurtle/14-standalone-spokesperson.png",
};

export function HurtlePoseIllustration({
  pose = "welcoming",
  className = "",
}: {
  pose?: "welcoming" | "guiding" | "waiting";
  className?: string;
}) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={POSE_SRC[pose]}
      alt={`Hurtle Turtle in ${pose} pose`}
      className={className}
      style={{
        width: "100%",
        height: "auto",
        maxHeight: 260,
        objectFit: "contain",
        borderRadius: 12,
        display: "block",
      }}
    />
  );
}
