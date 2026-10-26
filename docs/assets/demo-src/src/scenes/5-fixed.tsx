import {Layout, Rect, Txt, makeScene2D} from '@motion-canvas/2d';
import {all, createRef, easeOutCubic, fadeTransition, waitFor} from '@motion-canvas/core';
import {C, H, MONO, SANS, W, caption, mark, progress, shotCard} from '../lib';
import resolved from '../../shots/group-resolved.png';
import triage from '../../shots/triage-resolved.png';

// Scene 5: the doc is fixed and the group is resolved, then the end card.
const titleCrop = {x: 100, y: 200, w: 1400, h: 340};
const noteCrop = {x: 40, y: 1212, w: 800, h: 98};
const statusCrop = {x: 40, y: 616, w: 360, h: 90};

export default makeScene2D(function* (view) {
  view.fill(C.paper);
  yield* fadeTransition(0.45);
  const cap = createRef<Txt>();
  const title = createRef<Rect>();
  const note = createRef<Rect>();
  const status = createRef<Rect>();
  const body = createRef<Layout>();
  const end = createRef<Layout>();
  const endBg = createRef<Rect>();
  const fade = createRef<Rect>();

  view.add(
    <>
      {caption(cap, 'The owner adds the guide and marks it resolved.', C.dark)}
      <Layout ref={body}>
        {shotCard(title, resolved, 2560, 1440, titleCrop, 1000)}
        {shotCard(note, triage, 874, 1934, noteCrop, 700)}
        {shotCard(status, triage, 874, 1934, statusCrop, 380)}
      </Layout>
      {progress(5, false)}
      <Rect ref={endBg} width={W} height={H} fill={C.paper} opacity={0} />
      <Layout ref={end} layout direction="column" alignItems="center" gap={34} opacity={0}>
        <Layout layout gap={26} alignItems="center">
          {mark(110)}
          <Txt text="Kebivo" fontFamily={SANS} fontWeight={800} fontSize={104} letterSpacing={-4} fill={C.dark} />
        </Layout>
        <Txt text="When your AI doesn’t know, Kebivo tells the people who can fix it." fontFamily={SANS} fontWeight={700} fontSize={40} lineHeight={48} letterSpacing={-1} fill={C.muted} textAlign="center" width={900} textWrap />
        <Rect layout fill={C.ink} radius={12} padding={[16, 30]}>
          <Txt text="kebivo.com" fontFamily={MONO} fontWeight={700} fontSize={36} fill={C.mint} />
        </Rect>
        <Txt text="Open source · Self-hosted · One container" fontFamily={SANS} fontWeight={700} fontSize={22} fill={C.muted} />
      </Layout>
      <Rect ref={fade} width={W} height={H} fill={C.ink} opacity={0} />
    </>,
  );
  title().position([0, 70]).opacity(0);
  note().position([-230, 280]).opacity(0);
  status().position([380, 280]).opacity(0);
  title().y(60);

  yield* all(title().opacity(1, 0.4), title().y(40, 0.6, easeOutCubic));
  yield* waitFor(1.0);
  yield* all(note().opacity(1, 0.4), note().y(250, 0.5, easeOutCubic));
  yield* waitFor(0.5);
  yield* all(status().opacity(1, 0.4), status().y(250, 0.5, easeOutCubic));
  yield* waitFor(1.6);
  yield* all(endBg().opacity(1, 0.4), end().opacity(1, 0.4));
  yield* waitFor(2.8);
  // Fade to the dark first frame so the loop restarts cleanly.
  yield* fade().opacity(1, 0.5);
});
