"use client";

import { HurtleGuide } from "./HurtleGuide";
import React, { useState, useEffect, useRef } from "react";
import { ConciergeMark } from "./ConciergeMark";
import { Icon } from "./Icons";
import { SAMPLE_STORY as SAMPLE } from "../lib/api";

interface Step2YourSideProps {
  story: string;
  setStory: (value: string) => void;
  owner: string | null;
  setOwner: (value: string) => void;
  photo: boolean | null;
  setPhoto: (value: boolean) => void;
  onComplete: (data: { story: string; owner: string; photo: boolean }) => void;
  setActionBar: (sub: React.ReactNode, buttons: React.ReactNode) => void;
  onRulesClick: () => void;
  // Real flow: after the questions, this form (contact and photos) replaces the sample step.
  liveSlot?: React.ReactNode;
}

export function Step2YourSide({
  story,
  setStory,
  owner,
  setOwner,
  photo,
  setPhoto,
  onComplete,
  setActionBar,
  onRulesClick,
  liveSlot,
}: Step2YourSideProps) {
  const live = liveSlot !== undefined;
  const unsupported = live && Array.from(story).some(character => {
    const code = character.codePointAt(0) ?? 0;
    return code > 255 || (code < 32 && ![9, 10, 12].includes(code));
  });
  const restored = live && !unsupported && Boolean(owner && story.trim());
  const [messages, setMessages] = useState<Array<{ sender: "c" | "u" | "g"; text?: string; html?: React.ReactNode }>>([
    {
      sender: "c",
      text: live
        ? "Tell us what happened in your own words. We keep your exact wording and never invent facts or arguments."
        : "Thanks—all five details on your ticket are confirmed. Now tell us what happened in your own words.",
    },
    ...(restored
      ? [
          { sender: "u" as const, text: story },
          { sender: "c" as const, text: "Your saved account and ownership answer are ready below. Finish your contact and return-address details to continue." },
        ]
      : []),
  ]);
  const [stage, setStage] = useState<"story" | "owner" | "photo" | "done">(restored ? "done" : "story");
  const [storyErr, setStoryErr] = useState<boolean>(false);
  const composerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (stage === "story") {
      setActionBar(
        "We keep your exact wording and never invent arguments",
        <button
          className="btn btn-primary"
          type="button"
          id="actSendStory"
          onClick={() => {
            const val = story.trim();
            if (!val || unsupported) {
              setStoryErr(true);
              requestAnimationFrame(() => {
                const storyEl = document.getElementById("story");
                composerRef.current?.scrollIntoView({ block: "center", behavior: "instant" });
                storyEl?.focus({ preventScroll: true });
              });
              return;
            }
            setStoryErr(false);
            setMessages((prev) => [
              ...prev,
              { sender: "u", text: val },
              {
                sender: "c",
                text: live
                  ? "Thank you. Were you the registered owner of the vehicle on the date of the ticket?"
                  : "Thank you. Were you the registered owner of the vehicle on October 2, 2026?",
              },
            ]);
            setStage("owner");
          }}
        >
          Send my account →
        </button>
      );
    } else if (stage === "owner") {
      setActionBar("Next: choose your relationship to the vehicle above", null);
    } else if (stage === "photo") {
      setActionBar("Next: choose whether to include a supporting photo above", null);
    } else if (stage === "done" && live) {
      setActionBar(null, null);
    } else if (stage === "done") {
      setActionBar(
        "Saved · Step 2 complete",
        <button
          className="btn btn-primary"
          type="button"
          data-go={3}
          onClick={() => onComplete({ story: story || SAMPLE, owner: owner ?? "unsure", photo: photo ?? false })}
        >
          Start the research →
        </button>
      );
    }
  }, [stage, story, owner, photo, onComplete, setActionBar, live, unsupported]);

  const handleAskGuardrail = () => {
    setMessages((prev) => [
      ...prev,
      { sender: "u", text: "Will this work?" },
      {
        sender: "g",
        html: (
          <div>
            <div className="gl">
              <Icon id="shield" size={14} /> Clerical boundary
            </div>
            We can’t predict how the agency will decide, and we won’t judge your case. What we
            do: format your own words, check the mailing requirements, and show you the exact packet before you authorize mailing.{" "}
            <button
              type="button"
              className="chip"
              onClick={onRulesClick}
              style={{ marginTop: "8px" }}
            >
              What we never do
            </button>
          </div>
        ),
      },
      { sender: "c", text: "Whenever you’re ready, tell us what happened." },
    ]);
  };

  const handleChooseOwner = (val: string, label: string) => {
    setOwner(val);
    const nextMsgs: Array<{ sender: "c" | "u" | "g"; text?: string; html?: React.ReactNode }> = [
      ...messages,
      { sender: "u", text: label },
    ];
    if (val !== "yes") {
      nextMsgs.push({
        sender: "c",
        text: "Thanks for saying so. We’ll leave ownership out of the letter rather than guess.",
      });
    }
    if (live) {
      nextMsgs.push({ sender: "c", text: "Saved. Now add your return address and contact details for the letter." });
      setMessages(nextMsgs);
      setStage("done");
      return;
    }
    if (photo) {
      nextMsgs.push({ sender: "c", text: "Your sample sign photo is already included. Next, check the agency’s filing rules." });
      setMessages(nextMsgs);
      setStage("done");
      return;
    }
    const mentions = /photo|picture|pic\b|image/i.test(story);
    nextMsgs.push({
      sender: "c",
      text: mentions
        ? "You mentioned a photo. Do you want it kept in your evidence record?"
        : "Do you have a photo you’d like kept in your evidence record?",
    });
    setMessages(nextMsgs);
    setStage("photo");
  };

  const handleChoosePhoto = (val: boolean, label: string) => {
    setPhoto(val);
    const nextMsgs: Array<{ sender: "c" | "u" | "g"; text?: string; html?: React.ReactNode }> = [
      ...messages,
      {
        sender: "u",
        html: val ? (
          <span className="photo">
            <svg width="52" height="52" viewBox="0 0 72 72" aria-hidden="true">
              <rect width="72" height="72" fill="#cfe3f3" />
              <rect y="52" width="72" height="20" fill="#b9b2a5" />
              <rect x="34" y="18" width="4" height="40" fill="#6b6b6b" />
              <rect x="20" y="14" width="32" height="20" rx="2" fill="#fff" stroke="#b91c1c" strokeWidth="2" />
              <g fill="#2f7d3a">
                <ellipse cx="22" cy="18" rx="10" ry="6" />
                <ellipse cx="34" cy="22" rx="11" ry="7" />
                <ellipse cx="46" cy="20" rx="9" ry="6" />
              </g>
            </svg>
            <span>
              photo_of_sign.jpg<small>Uploaded · 1 photo</small>
            </span>
          </span>
        ) : (
          label
        ),
      },
    ];
    if (val) {
      nextMsgs.push({
        sender: "c",
        text: "Got it. We’ll print it after your letter, labelled as uploaded by you, and keep a copy in your record.",
      });
    }
    nextMsgs.push({
      sender: "c",
      text: "That’s everything we need from you. Next we’ll read the City of San Diego’s official pages and show you every fact with its source.",
    });
    setMessages(nextMsgs);
    setStage("done");
  };

  return (
    <section className="screen on" id="s2" aria-labelledby="h-s2">
      <div className="voice">
        <HurtleGuide pose="story" />
        <div>
          <p className="eyebrow">Step 2 · Your side</p>
          <h2 id="h-s2" tabIndex={-1}>
            Tell us what happened.
          </h2>
          <p className="lead">
            Plain facts in your own voice—times, signs, meter screens, receipts, or markings you observed.
          </p>
        </div>
      </div>

      <div className="chat" id="chatlog" aria-live="polite" aria-relevant="additions text">
        {messages.map((m, i) => (
          <div key={i} className={m.sender === "u" ? "row-u" : "row-c"}>
            {m.sender !== "u" && <ConciergeMark size={30} state="idle" />}
            <div className={`msg ${m.sender}`}>
              {m.html ? m.html : m.text}
            </div>
          </div>
        ))}
      </div>

      {stage === "story" && (
        <div className="composer is-next-task" id="composer" ref={composerRef}>
          <form
            id="storyForm"
            onSubmit={(e) => {
              e.preventDefault();
              document.getElementById("actSendStory")?.click();
            }}
          >
            <label className="composer-label" htmlFor="story">
              <span>Your side, in your own words</span>
              <span className="status-pill is-next">Required</span>
            </label>
            <textarea
              id="story"
              aria-invalid={storyErr}
              aria-describedby="storyErr storyHelp storyCompletenessPrompt"
              placeholder="What happened? Write it plainly, the way you’d explain it to a clerk."
              value={story}
              onChange={(e) => { setStory(e.target.value); setStoryErr(false); }}
            />
            <p className="err" id="storyErr" role="alert">
              {storyErr
                ? unsupported
                  ? "Replace characters the letter cannot print. Use straight quotes or apostrophes in place of curly ones, and use a plain hyphen in place of a long dash."
                  : live
                    ? "Enter your reason in your own words."
                    : "Write a sentence or two, or tap “Use sample answer”."
                : ""}
            </p>
            <p className="dictate-tip" id="storyHelp">Tip: You can tap your phone keyboard’s microphone to dictate.</p>
            <p className="muted small" id="storyCompletenessPrompt">
              Include relevant dates, times, locations, or sign details. We format your exact account without adding arguments or judging your case.
            </p>
            <div className="crow">
              {!live && (
                <button
                  type="button"
                  className="chip"
                  onClick={() => {
                    setStory(SAMPLE);
                    setStoryErr(false);
                  }}
                >
                  Use sample answer
                </button>
              )}
              <button
                type="button"
                className="chip"
                onClick={handleAskGuardrail}
              >
                Ask: “Will this work?”
              </button>
            </div>
          </form>
        </div>
      )}

      {stage === "owner" && (
        <div className="composer is-next-task" id="composer">
          <p className="eyebrow" style={{ marginBottom: "8px" }}>Next · Registered owner check</p>
          <div className="choices" role="group" aria-label="Tap one answer above">
            {[
              ["yes", "Yes"],
              ["no", "No"],
              ["unsure", "I’m not sure"],
            ].map(([v, l]) => (
              <button
                key={v}
                type="button"
                className="btn btn-ghost"
                onClick={() => handleChooseOwner(v, l)}
              >
                <span>{l}</span>
                <span aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {stage === "done" && live && (
        <div className="saved-statement-bar">
          <span className="status-pill is-saved"><Icon id="check" size={12} /> Account &amp; ownership saved</span>
          <button type="button" className="chip" onClick={() => setStage("story")}>
            Edit my account or ownership answer
          </button>
        </div>
      )}
      {stage === "done" && liveSlot}

      {stage === "photo" && (
        <div className="composer is-next-task" id="composer">
          <div className="choices" role="group" aria-label="Tap one answer above">
            {[
              [true, "Yes, save it"],
              [false, "No photo"],
            ].map(([v, l]) => (
              <button
                key={String(v)}
                type="button"
                className="btn btn-ghost"
                onClick={() => handleChoosePhoto(Boolean(v), String(l))}
              >
                <span>{String(l)}</span>
                <span aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
