Third-party files bundled with Cue's web interface, so it never loads code from a CDN.

hls.min.js
  hls.js 1.7.3 (https://github.com/video-dev/hls.js), dist/hls.min.js from the npm package
  https://registry.npmjs.org/hls.js/-/hls.js-1.7.3.tgz
  npm integrity: sha512-MsPlx6yVW4Qv4C7mEVou4/gk/5cN2dVxOTnrNjPSmyH2f0Ln+/UkG10Ax1PE6OvrmTVPKgQYozTtBaPOuCzq7A==
  sha256 of hls.min.js: a12e7ee1cd64a69dcdb314157e45dafcba705bfb0b1440b7935cb265d374423e
  License: Apache License 2.0, see hls.js-LICENSE.txt

To update it: npm pack hls.js@<version>, check the integrity against `npm view hls.js@<version> dist.integrity`,
copy package/dist/hls.min.js and package/LICENSE here, and update this file.
