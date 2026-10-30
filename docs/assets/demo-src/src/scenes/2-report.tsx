import {Layout, Rect, Txt, makeScene2D} from '@motion-canvas/2d';
import {all, createRef, waitFor} from '@motion-canvas/core';
import {fadeTransition} from '@motion-canvas/core';
import {C, H, MONO, SANS, W, caption, progress} from '../lib';
import result from '../../shots/mcp-result.json';

// Scene 2: the agent reports the gap over MCP. `status` and `event_id` come from a real response.
export default makeScene2D(function* (view) {
  view.fill(C.paper);
  yield* fadeTransition(0.45);
  const cap = createRef<Txt>();
  const card = createRef<Rect>();
  const l1 = createRef<Txt>();
  const l2 = createRef<Txt>();
  const l3 = createRef<Txt>();
  const l4 = createRef<Txt>();
  const ok = createRef<Txt>();
  const mono = (ref: ReturnType<typeof createRef<Txt>>, fill: string, size = 30, weight = 500) => (
    <Txt ref={ref} text="" fontFamily={MONO} fontWeight={weight} fontSize={size} fill={fill} />
  );

  view.add(
    <>
      {caption(cap, 'Your agent can report the gap over MCP.', C.dark)}
      <Rect
        ref={card}
        y={90}
        width={1120}
        height={330}
        radius={16}
        fill={C.ink}
        shadowColor="rgba(20,32,27,0.3)"
        shadowBlur={40}
        shadowOffsetY={16}
        opacity={0}
        layout
        direction="column"
        padding={[34, 44]}
        gap={14}
      >
        <Txt text="AI AGENT, CONNECTED OVER MCP" fontFamily={SANS} fontWeight={700} fontSize={18} letterSpacing={3} fill="#7fa493" />
        {mono(l1, '#f5d98a', 32, 700)}
        {mono(l2, '#e7f1ec')}
        {mono(l3, '#e7f1ec')}
        <Layout layout gap={18} alignItems="center" marginTop={10}>
          <Rect ref={ok} fill={C.green} radius={10} padding={[8, 20]} opacity={0}>
            <Txt text={`status: ${result.status}`} fontFamily={MONO} fontWeight={700} fontSize={32} fill="#fff" />
          </Rect>
          {mono(l4, '#8fb3a3', 22)}
        </Layout>
      </Rect>
      {progress(2, false)}
    </>,
  );

  yield* card().opacity(1, 0.3);
  yield* l1().text('tools/call  report_knowledge_gap', 0.9);
  yield* l2().text('question: "Do you ship to Canada?"', 1.0);
  yield* l3().text('tags:     shipping', 0.5);
  yield* waitFor(0.4);
  yield* all(ok().opacity(1, 0.25), l4().text(`event_id ${result.event_id.slice(0, 8)}…`, 0.5));
  yield* waitFor(1.6);
});
