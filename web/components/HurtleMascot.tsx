import React from "react";

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
  welcoming: "/images/hurtle/welcome-transparent.png",
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
