import { ATTORNEY_DISCLOSURE } from "../components/LegalDisclosure";
import { mkdtempSync, openSync, readFileSync, closeSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { expect, test } from "@playwright/test";

// Real React server rendering and pure decisions run without browser/socket support.
// Interaction/layout acceptance lives in service-quality-browser.spec.ts.
function execute(module: string, name: string, props: object, render = true): string {
  const source = `
    const fs = require('node:fs'); const ts = require('typescript');
    for (const extension of ['.ts','.tsx']) require.extensions[extension] = (module, filename) => {
      module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'), {
        compilerOptions: {module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}
      }).outputText,filename);
    };
    const target = require(require('node:path').resolve(process.env.T37_SOURCE_ROOT || '.',process.argv[1]))[process.argv[2]];
    const props = JSON.parse(process.argv[3]);
    const result = process.argv[4] === 'render'
      ? require('react-dom/server').renderToStaticMarkup(require('react').createElement(target,props))
      : JSON.stringify(target(...props.args));
    process.stdout.write(result);
  `;
  const directory = mkdtempSync(join(tmpdir(), "pb-t37-"));
  const stdout = openSync(join(directory,"out"),"w");
  const stderr = openSync(join(directory,"err"),"w");
  try {
    const result = spawnSync(process.execPath,["-e",source,module,name,JSON.stringify(props),render ? "render" : "call"],{cwd:process.cwd(),encoding:"utf8",stdio:["ignore",stdout,stderr]});
    expect(result.error).toBeUndefined();
    expect(result.status,readFileSync(join(directory,"err"),"utf8")).toBe(0);
    return readFileSync(join(directory,"out"),"utf8");
  } finally { closeSync(stdout); closeSync(stderr); rmSync(directory,{recursive:true}); }

}
const render = (name: string, props: object) => execute(`components/${name}.tsx`,name,props);
const decide = (file: string, name: string, ...args: unknown[]) => JSON.parse(execute(`lib/${file}.ts`,name,{args},false));
const contact = {issuedCity:"San Francisco",issuedState:"CA",issuerKind:"GOVERNMENT",name:"Alex Example",phone:"415-555-0100",email:"alex@example.test",line1:"123 Example Street",line2:"",city:"San Francisco",state:"CA",zip:"94103"};
const details = {busy:false,submitting:false,error:null,values:contact,ticketPhoto:null,ticketBackPhoto:null,evidencePhotos:[],ticketPhotoRef:"photo-ticket",ticketBackPhotoRef:null};

test.describe("M1 optional photos and intake", () => {
  test("guide allows zero photos, separates citation capture and describes safe chosen enclosures", () => {
    const html = render("LiveDetails",details);
    for (const phrase of ["Ticket in place (optional)","Surroundings (optional)","A detail from your statement (optional)","From a safe place","continue without","photos you choose will be mailed"]) expect(html).toContain(phrase);
    expect(html).toContain('id="liveTicketPhoto"');
    expect(html).toContain('id="liveEvidencePhoto"');
    expect(html).not.toMatch(/(?:need|require|must have) (?:exactly )?three photos/i);
    expect(html).not.toMatch(/prove (?:your|a) (?:case|defen[cs]e)/i);
  });
  test("existing selected photos expose accessible ordering and removal with escaped names", () => {
    const html = render("LiveDetails",{...details,evidencePhotos:[{id:"one",ref:"photo-one",file:{name:"<private>.png"}},{id:"two",ref:"photo-two",file:null}]});
    expect(html).toContain('aria-label="Move supporting photo 2 up"');
    expect(html).toContain('aria-label="Move supporting photo 1 down"');
    expect(html).toContain('aria-label="Remove supporting photo 2"');
    expect(html).toContain('&lt;private&gt;.png');
    expect(html).toContain('aria-describedby="photoGuide"');
    expect(html).toContain('class="evidence-item"');
  });
  test("validation names every invalid control, accepts optional omissions and never trims customer account", () => {
    expect(decide("intake","contactErrors",contact,true)).toEqual({});
    expect(decide("intake","contactErrors",{...contact,issuedCity:"",issuedState:"C",name:"",phone:"no",email:"bad",line1:"",city:"",state:"?",zip:"no"},false)).toEqual({
      liveIssuedCity:expect.any(String),liveIssuedState:expect.any(String),liveName:expect.any(String),livePhone:expect.any(String),liveEmail:expect.any(String),liveLine1:expect.any(String),liveCity:expect.any(String),liveState:expect.any(String),liveZip:expect.any(String),liveTicketPhoto:expect.any(String),
    });
    expect(decide("intake","contactErrors",{...contact,line2:"",zip:"94103-1234"},true)).toEqual({});
  });
});

const status = {case_id:"case-synthetic",workspace_id:"workspace-synthetic",state:"IN_TRANSIT",payment_state:"AUTHORIZED",selected_tier:"CERTIFIED",amount_cents:2999,window_started_at:null,window_expires_at:null,seconds_remaining:null,window_status:null,question:null,stop:null,fake:true};
const tracking = {case_id:"case-synthetic",state:"IN_TRANSIT",tier:"CERTIFIED",tracking_number:null,events:[],delivery_confirmation_title:"ParkingBreaker Delivery Confirmation",fake:true};

test.describe("M2 authoritative fulfillment", () => {
  test("all post-authorization states resume without requesting another letter or hold", () => {
    for (const state of ["FROZEN","SUBMITTING","IN_TRANSIT","DELIVERED","RETURNED","CLOSED_RETURNED"]) {
      for (const payment_state of [null,"AUTHORIZED","CAPTURE_PENDING","CAPTURE_RETRYING","CAPTURED","WRITTEN_OFF","RELEASED","REFUNDED"]) {
        expect(decide("fulfillment","customerStage",{...status,state,payment_state})).toBe(7);
      }
    }
    expect(decide("fulfillment","customerStage",{...status,state:"STOPPED_NO_CHARGE"})).toBe(8);
    expect(decide("fulfillment","customerStage",{...status,state:"NEEDS_CUSTOMER"})).toBe(3);
    expect(decide("fulfillment","customerStage",{...status,state:"AWAITING_ADOPTION"})).toBe(6);
  });
  test("payment is independent of postal state and describes uncertainty without new charges", () => {
    expect(decide("fulfillment","paymentCopy",{...status,payment_state:"CAPTURE_PENDING"})).toMatch(/checking your existing payment/);
    expect(decide("fulfillment","paymentCopy",{...status,payment_state:"CAPTURE_RETRYING"})).toMatch(/existing card payment/);
    expect(decide("fulfillment","paymentCopy",{...status,payment_state:"CAPTURED"})).toContain("$29.99");
    expect(decide("fulfillment","paymentCopy",{...status,payment_state:"REFUNDED"})).toMatch(/refunded/i);
    expect(decide("fulfillment","paymentCopy",{...status,payment_state:null})).not.toMatch(/nothing was charged/i);
  });
  test("delivery needs matching saved tracking; production and late scans remain pending", () => {
    expect(decide("fulfillment","fulfillmentCopy",{...status,state:"FROZEN"},null).title).toMatch(/authorized/i);
    expect(decide("fulfillment","fulfillmentCopy",status,tracking).title).toMatch(/postal updates/i);
    expect(decide("fulfillment","fulfillmentCopy",{...status,state:"DELIVERED"},{...tracking,state:"DELIVERED",case_id:"other"}).title).not.toMatch(/delivered/i);
    expect(decide("fulfillment","fulfillmentCopy",{...status,state:"DELIVERED"},{...tracking,state:"DELIVERED"}).title).toBe("USPS reports delivery.");
  });
  test("returned-mail screen shows only recorded refund and mailing evidence", () => {
    const html = render("LiveFulfillment",{status:{...status,state:"RETURNED",return_remedy:{attempt:1,kind:"FULL_REFUND",reason_code:"SYNTHETIC",message:"The packet was returned. Your original refund is pending.",refund_cents:2999,refund_status:"pending",remail:false,proof_packet:null}},tracking:{...tracking,state:"RETURNED"},busy:false,error:null});
    expect(html).toContain("Mail returned");
    expect(html).toContain("Refund pending");
    expect(html).not.toContain("Refund completed");
    expect(html.replaceAll(ATTORNEY_DISCLOSURE, "")).not.toMatch(/lawyer|legal advice|guaranteed|\bwin\b|dismissal rates/i);
    expect(html).not.toContain("dev-worker");
  });
  test("free beta copy states no card hold and replaces only the paid footer", () => {
    const free = {...status,state:"IN_TRANSIT",payment_state:"NO_CHARGE",amount_cents:0};
    expect(decide("fulfillment","paymentCopy",free)).toBe("Your selected beta appeal is free. No card hold or payment is required.");
    const html = render("LiveFulfillment",{status:free,tracking:{...tracking,case_id:free.case_id},busy:false,error:null});
    expect(html).toContain("Your selected beta appeal is free. No card hold or payment is required.");
    expect(html).toContain("This selected beta appeal is free. If something goes wrong, we will review the saved mailing record. No second mailing is automatically authorized.");
    expect(html).not.toContain("$0.00");
    expect(html).not.toMatch(/card hold authorized/i);
    expect(html).not.toContain("remain our responsibility to fix or refund");
    expect(html).not.toMatch(/lawyer|legal advice|guaranteed|\bwin\b|dismissal rates|free re-mail/i);
  });
  test("paid fulfillment keeps its paid footer, hold copy and record control", () => {
    const html = render("LiveFulfillment",{status,tracking,busy:false,error:null});
    expect(html).toContain("You can return using this appeal’s private link. Our mailing mistakes are ours to fix under the return policy.");
    expect(html).toContain("$29.99 card hold authorized.");
    expect(html).not.toContain("This selected beta appeal is free");
    const paidMailed = render("LiveFulfillment",{status:{...status,payment_state:"CAPTURED"},tracking,busy:false,error:null});
    expect(paidMailed).toContain("$29.99 card payment confirmed.");
    expect(paidMailed).toContain('id="livePrepareMailingRecord"');
  });
  test("free beta accepted and returned mail expose the original record download control", () => {
    for (const state of ["IN_TRANSIT","DELIVERED","RETURNED","CLOSED_RETURNED"]) {
      const html = render("LiveFulfillment",{status:{...status,state,payment_state:"NO_CHARGE",amount_cents:0},tracking:{...tracking,state,case_id:status.case_id},busy:false,error:null});
      expect(html,state).toContain('id="livePrepareMailingRecord"');
    }
  });
  test("no record download control for unconfirmed or mismatched tracking", () => {
    for (const state of ["INTAKE","RESEARCHING","NEEDS_CUSTOMER","DRAFTING","AWAITING_ADOPTION","FROZEN","SUBMITTING","STOPPED_NO_CHARGE"]) {
      const html = render("LiveFulfillment",{status:{...status,state,payment_state:"NO_CHARGE",amount_cents:0},tracking:{...tracking,state,case_id:status.case_id},busy:false,error:null});
      expect(html,state).not.toContain('id="livePrepareMailingRecord"');
      expect(html,state).not.toContain('id="liveMailingRecord"');
    }
    for (const state of ["IN_TRANSIT","DELIVERED","RETURNED","CLOSED_RETURNED"]) {
      const html = render("LiveFulfillment",{status:{...status,state,payment_state:"NO_CHARGE",amount_cents:0},tracking:{...tracking,state,case_id:"case-other"},busy:false,error:null});
      expect(html,`mismatch ${state}`).not.toContain('id="livePrepareMailingRecord"');
      expect(html,`mismatch ${state}`).not.toContain('id="liveMailingRecord"');
    }
    const paid = render("LiveFulfillment",{status,tracking,busy:false,error:null});
    expect(paid).not.toContain('id="livePrepareMailingRecord"');
    expect(paid).not.toContain('id="liveMailingRecord"');
  });
  test("a closed free-beta return shows the backend message without refund or re-mail promises", () => {
    const message = "Your free beta letter was returned. This free beta appeal is closed with no charge: nothing was charged, so there is no refund and no re-mail. Your original mailing record remains available.";
    const closed = {...status,state:"CLOSED_RETURNED",payment_state:"NO_CHARGE",amount_cents:0,return_remedy:{attempt:1,kind:"BETA_RETURN_CLOSED",reason_code:"BETA_RETURN_CLOSED",message,refund_cents:0,refund_status:null,remail:false,proof_packet:null}};
    const html = render("LiveFulfillment",{status:closed,tracking:{...tracking,state:"CLOSED_RETURNED"},busy:false,error:null});
    expect(html).toContain(message);
    expect(html).not.toContain("Refund pending");
    expect(html).not.toContain("Refund completed");
    expect(html).not.toContain("free re-mail");
    expect(html).not.toContain("No additional card authorization");
    expect(html).toContain("This selected beta appeal is free.");
    expect(html).toContain('id="livePrepareMailingRecord"');
    const stale = render("LiveFulfillment",{status:{...closed,return_remedy:{...closed.return_remedy,refund_cents:2999,refund_status:"pending",remail:true}},tracking:{...tracking,state:"CLOSED_RETURNED"},busy:false,error:null});
    expect(stale).toContain(message);
    expect(stale).not.toContain("Refund pending");
    expect(stale).not.toContain("free re-mail");
  });
  test("service explains the first USPS scan charge and contains no prohibited promotion", () => {
    const aside = render("DesktopAside",{servicePhase:"paid",funding:"paid"});
    const offer = render("Step4Offer",{selectedTier:"certified",liveOffer:{issuerName:"Synthetic agency",decidingBody:"Synthetic review office",standardOffered:false,testMode:false,certifiedCents:2999,standardCents:null},cardSlot:"Card entry"});
    expect(offer).toContain(ATTORNEY_DISCLOSURE);
    for (const html of [aside,offer]) {
      expect(html).toContain("first USPS scan");
      expect(html.replaceAll(ATTORNEY_DISCLOSURE, "")).not.toMatch(/lawyer|legal advice|guaranteed|\bwin\b|dismissal rates/i);
    }
  });
});

test.describe("M3 exact words and customer control", () => {
  const draft = {case_id:"case-synthetic",revision:1,text:"My own words.\nA second fact.",text_sha256:"a".repeat(64),adopted_sha256:null,adoption_screen_text:"I adopt these exact final words as my own statement.",spans:[],flags:[]};
  const props = {draft,busy:false,error:null,preview:null,previewUrl:null};
  test("letter offers an explicit refusal before adoption and displays the exact adoption disclosure", () => {
    const html = render("LiveLetter",props);
    expect(html).toContain('id="liveRefuse"');
    expect(html).toContain("Do not mail this letter");
    expect(html).toContain(draft.adoption_screen_text);
    expect(html).toContain("My own words.\nA second fact.");
    expect(html).not.toContain('id="liveApprove"');
  });
  test("adopted letter offers a keyboard signature image path beside the same packet gate", () => {
    const html = render("LiveLetter",{...props,draft:{...draft,adopted_sha256:draft.text_sha256}});
    expect(html).toContain('id="liveSignatureFile"');
    expect(html).toContain('type="file"');
    expect(html).toContain('accept="image/png"');
    expect(html).toContain('aria-label="Draw your signature"');
    expect(html).toContain('id="livePreview"');
    expect(html).not.toContain('id="liveApprove"');
  });
});

test.describe("M4 honest entry and accessible writing", () => {
  test("actual manual ticket entry never claims automatic photo reading", () => {
    const html = render("Step1Ticket",{live:true,fields:[],plate:"",plateState:"",backPhoto:false});
    expect(html).toContain("Enter each detail as printed on your ticket");
    expect(html).not.toContain("I’ll read it");
    expect(html).not.toContain("You confirm every detail I read");
    expect(html).not.toContain("Add sample back photo");
  });
  test("account input links instructions and invalid state to announced errors", () => {
    const html = render("Step2YourSide",{story:"",owner:null,photo:null,liveSlot:"Contact form"});
    expect(html).toContain('aria-describedby="storyHelp storyErr"');
    expect(html).toContain('aria-invalid="false"');
    expect(html).toContain('id="storyErr"');
    expect(html).toContain('role="alert"');
    expect(html).not.toContain("make sure");
  });
});

test("M2 recovery: uncertain destination responses never assert that nothing was saved", () => {
  for (const detail of ["network_unavailable","request_failed"]) {
    const message: string = JSON.parse(execute("components/DestinationChoice.tsx","destinationError",{args:[detail]},false));
    expect(message).toMatch(/couldn.t confirm/i);
    expect(message).not.toMatch(/nothing was (saved|accepted|changed)/i);
    expect(message).toMatch(/saved status/i);
  }
});

test.describe("T37 accessible names contain their visible text", () => {
  const header = {step:7,timerEnd:null,now:0,lapsed:false,evidenceCount:"13/16",onBrandClick:()=>{},onEvidenceClick:()=>{},onRulesClick:()=>{}};
  test("header evidence button names the visible count and keeps the checklist name", () => {
    const html = render("Header",header);
    expect(html).toContain('aria-label="Evidence 13/16: official evidence checklist"');
    expect(html).toMatch(/Evidence<\/span> <span class="badge-ct"/);
  });
  test("photo order buttons show text that their accessible names contain", () => {
    const html = render("LiveDetails",{...details,evidencePhotos:[{id:"one",ref:"photo-one",file:null},{id:"two",ref:"photo-two",file:null}]});
    expect(html).toMatch(/aria-label="Move supporting photo 2 up"[^>]*>Up<\/button>/);
    expect(html).toMatch(/aria-label="Move supporting photo 1 down"[^>]*>Down<\/button>/);
  });
});
