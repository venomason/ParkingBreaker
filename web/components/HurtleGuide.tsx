import Image from "next/image";

const portraits = {
  welcome: "welcome-transparent",
  ticket: "photographing-a-ticket",
  story: "listening",
  letter: "offering-a-letter",
} as const;

export function HurtleGuide({ pose, cover = false }: {
  pose: keyof typeof portraits;
  cover?: boolean;
}) {
  return <Image
    className={cover ? "guide-cover-art" : "guide-task-art"}
    src={`/images/hurtle/${portraits[pose]}.png`}
    alt={cover ? (pose === "ticket" ? "Hurtle Turtle photographs a parking ticket." : "Hurtle Turtle welcomes you with an open hand.") : ""}
    width={1254}
    height={1254}
    sizes={cover
      ? "(min-width: 1024px) 400px, (min-width: 600px) 340px, 64vw"
      : "(min-width: 600px) 272px, 50vw"}
    loading={cover ? "eager" : "lazy"}
  />;
}
