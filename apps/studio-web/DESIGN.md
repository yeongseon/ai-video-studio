# Studio Web — existing surface contract

## 1. Scope
Functional first-short extraction. Preserve the existing inline styles, layout,
copy hierarchy, and reusable components. No redesign or new visual theme.

## 2. Color
Existing create surface: primary #4285f4, disabled #93b4f4, text #555/#666,
demo panel #f0f7ff with #cfe3ff border. Errors use #fef2f2, #fca5a5, #b91c1c.
Use the existing Button and Card variant colors for added review controls.

## 3. Typography
Inherit application font. Existing heading sizes 24/18/16, body 14/13, auxiliary
12/11 pixels. Preserve monospace JSON editing.

## 4. Layout
Create page: 720px maximum, centered, 24px padding. Project: 1200px with a run,
960px without. Existing spacing increments 4/8/12/16/24/32. Preserve responsive
intrinsic sizing and the preview's output aspect ratio.
Timeline preview height is capped at 60vh; width is the smaller of the container
width and 60vh multiplied by the output aspect ratio. Demo actions wrap below
their description on narrow viewports; long disclosure text wraps within its card.
App navigation wraps into rows with 8px row gaps, retaining 24px horizontal padding
and a 48px minimum height. Progress and confirmation dialogs include padding in
their border-box width and stay within the viewport minus 32px. Existing desktop
outer widths (440px progress, 468px confirmation) and visual styling are retained.

## 5. Primitives and states
Reuse Card (default/outlined/dashed) and Button variants. Preserve loading,
disabled, alert, empty, running, failed, and completed states. Demo disclosure
uses the existing blue panel; timeline review uses Card and TimelinePreview.
Only an absent run receives the existing dashed empty card.

## 6. Motion
Preserve current Button opacity transition and ProgressDialog spinner.
No new decorative motion.

## 7. Accessibility
Native buttons with disabled state, labelled form fields and tabs, errors with
role=alert. Keep ProgressDialog focus trapping and focus restoration. Saved
timeline approval is explicit and disabled until preview and revision load.
Readiness requires the first image load or a decoded video frame at the trimmed
source target after seeking (within two source frames at 30 fps). Approval uses
the revision from that preview response; failed media revokes readiness and shows
reload/access guidance. Onboarding guidance is labelled "For AI generation:".
Final review retains the existing video card and adds an authenticated download
link plus an explicit "Approve final & publish" button. Publishing has disabled,
error/retry, and confirmed Published states. Timeline runs show a wrapping four-step
workflow (Timeline review, Render, Final review, Published) with aria-current,
instead of claiming legacy generation stages completed. Paused review gates use
their review actions rather than generic Resume; failure/cancellation recovery stays.

## 8. Verification and accepted debt
Component/API regression tests cover state and wire contracts. Parent lane owns
real-backend browser verification at responsive sizes. Legacy inline style
values are retained during extraction; design-token migration is outside this
functional change. No browser or Lighthouse pass is claimed here.

### Timeline clock fix (#899)
Primary action: Approve timeline & render. Priorities: visible frame, transport
position, fidelity limits. Required content: preview stage, Play/Pause, labelled
seek, time readout, revision, approval and reload/access errors. Left out on
purpose: extra playback speeds, decorative controls and render-parity claims.
The logical timeline owns time; video follows trim offsets, holds the clock
while seeking/buffering and resyncs above 2/30 seconds drift. Hidden pages pause.
Audio, captions, transitions and possible framing differences are disclosed.
