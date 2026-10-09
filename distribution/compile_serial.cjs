'use strict';
// Run the exact pinned upstream compile constituents sequentially, without
// changing upstream files or omitting typechecks. NODE_OPTIONS remains caller-owned.
const path = require('node:path');
const {createRequire} = require('node:module');
const source = path.resolve(process.argv[2]);
process.chdir(source);
const upstream = createRequire(path.join(source, 'package.json'));
upstream('./build/gulpfile.js');
const gulp = upstream('gulp');
const task = upstream('./build/lib/task.js');
const {monacoTypecheckTask} = upstream('./build/gulpfile.editor.js');
task.series(monacoTypecheckTask, gulp.task('compile-client'),
  gulp.task('compile-extensions'), gulp.task('compile-extension-media'))()
  .catch(error => { console.error(error); process.exitCode = 1; });
