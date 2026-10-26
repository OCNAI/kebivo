import {makeProject} from '@motion-canvas/core';
import './fonts.css';
import problem from './scenes/1-problem?scene';
import report from './scenes/2-report?scene';
import route from './scenes/3-route?scene';
import group from './scenes/4-group?scene';
import fixed from './scenes/5-fixed?scene';

// Canvas text needs the fonts loaded before the first frame is drawn.
await Promise.all(
  ['400 20px Inter', '700 20px Inter', '800 20px Inter', '500 20px "JetBrains Mono"', '700 20px "JetBrains Mono"'].map(f =>
    document.fonts.load(f),
  ),
);

export default makeProject({scenes: [problem, report, route, group, fixed]});
