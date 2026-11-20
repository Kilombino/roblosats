import path from 'path';
import { Compiler, Configuration, DefinePlugin } from 'webpack';
import CopyWebpackPlugin from 'copy-webpack-plugin';
import HtmlWebpackPlugin from 'html-webpack-plugin';
import { version } from './package.json';
import { Buffer } from 'buffer';
import fs from 'fs-extra';

// Declare __dirname for TypeScript
declare const __dirname: string;

const outputPaths: string[] = [
  path.resolve(__dirname, '../nodeapp/static'),
  path.resolve(__dirname, '../desktopApp/static'),
  path.resolve(__dirname, '../web/static'),
];

const config: Configuration = {
  entry: './src/index.js',
  module: {
    rules: [
      {
        test: /\.(ts|js)x?$/,
        exclude: /node_modules/,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env', '@babel/preset-react', '@babel/preset-typescript'],
          },
        },
      },
      {
        test: /\.css$/,
        use: ['style-loader', 'css-loader'],
      },
    ],
  },
  experiments: { asyncWebAssembly: true },
  resolve: {
    extensions: ['.tsx', '.ts', '.jsx', '.js'],
  },
};

const configNode = (env: any, _argv: { mode: string }): Configuration => {
  return {
    ...config,
    output: {
      path: path.resolve(__dirname, 'static/frontend'),
      filename: `main.v${version}.[contenthash].js`,
      clean: true,
      publicPath: 'auto',
    },
    plugins: [
      // Django
      new HtmlWebpackPlugin({
        publicPath: '/static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: false,
          mobile: false,
        },
        filename: path.resolve(__dirname, 'templates/frontend/basic.html'),
        inject: 'body',
        robosatsSettings: 'web-basic',
        basePath: '/',
      }),
      new HtmlWebpackPlugin({
        publicPath: '/static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: true,
          mobile: false,
        },
        filename: path.resolve(__dirname, 'templates/frontend/pro.html'),
        inject: 'body',
        robosatsSettings: 'web-pro',
        basePath: '/',
      }),
      // Node App
      new HtmlWebpackPlugin({
        publicPath: '/static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: false,
          mobile: false,
        },
        filename: path.resolve(__dirname, '../nodeapp/basic.html'),
        inject: 'body',
        robosatsSettings: 'selfhosted-basic',
        basePath: '/',
      }),
      new HtmlWebpackPlugin({
        publicPath: '/static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: true,
          mobile: false,
        },
        filename: path.resolve(__dirname, '../nodeapp/pro.html'),
        inject: 'body',
        robosatsSettings: 'selfhosted-pro',
        basePath: '/',
      }),

      // Desktop App
      new HtmlWebpackPlugin({
        publicPath: './static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: false,
          mobile: false,
        },
        filename: path.resolve(__dirname, '../desktopApp/index.html'),
        inject: 'body',
        robosatsSettings: 'desktop-basic',
        basePath: '/',
      }),

      // Web App
      new HtmlWebpackPlugin({
        publicPath: '/static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: false,
          mobile: false,
        },
        filename: path.resolve(__dirname, '../web/basic.html'),
        inject: 'body',
        robosatsSettings: 'web-basic',
        basePath: '/',
      }),
      new HtmlWebpackPlugin({
        publicPath: '/static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: true,
          mobile: false,
        },
        filename: path.resolve(__dirname, '../web/pro.html'),
        inject: 'body',
        robosatsSettings: 'web-pro',
        basePath: '/',
      }),

      {
        apply: (compiler: Compiler) => {
          compiler.hooks.afterEmit.tapAsync('CopyFilesPlugin', (_compilation, callback) => {
            const federationSrc = path.resolve(__dirname, 'static/federation.json');
            const federationDest = path.resolve(__dirname, '../api/federation.json');

            Promise.all([
              // Copy the full static directory to each platform output path.
              ...outputPaths.map((outputPath) => {
                const sourceDir = path.resolve(__dirname, 'static');
                return fs
                  .copy(sourceDir, outputPath)
                  .then(() => {
                    console.log(`Files copied to ${outputPath}`);
                  })
                  .catch((err) => {
                    console.error(`Error copying files to ${outputPath}:`, err);
                  });
              }),
              // Copy federation.json to api/ so the backend serves the same
              // file that is committed to the repository.
              fs
                .copy(federationSrc, federationDest)
                .then(() => {
                  console.log(`federation.json copied to ${federationDest}`);
                })
                .catch((err) => {
                  console.error(`Error copying federation.json to api/:`, err);
                }),
            ])
              .then(() => {
                callback();
              })
              .catch(callback);
          });
        },
      },
    ],
  };
};

const configAndroid = (env: any, _argv: { mode: string }): Configuration => {
  return {
    ...config,
    module: {
      ...config.module,
      rules: [
        ...(config?.module?.rules || []),
        {
          test: path.resolve(__dirname, 'src/i18n/Web.js'),
          loader: 'file-replace-loader',
          options: {
            condition: 'if-replacement-exists',
            replacement: path.resolve(__dirname, 'src/i18n/Mobile.js'),
            async: true,
          },
        },
        {
          test: path.resolve(__dirname, 'src/geo/Web.js'),
          loader: 'file-replace-loader',
          options: {
            condition: 'if-replacement-exists',
            replacement: path.resolve(__dirname, 'src/geo/Mobile.js'),
            async: true,
          },
        },
        {
          test: path.resolve(__dirname, 'src/services/Roboidentities/Web.ts'),
          loader: 'file-replace-loader',
          options: {
            condition: 'if-replacement-exists',
            replacement: path.resolve(__dirname, 'src/services/Roboidentities/Android.ts'),
            async: true,
          },
        },
        {
          test: path.resolve(__dirname, 'src/components/RobotAvatar/placeholder.json'),
          loader: 'file-replace-loader',
          options: {
            condition: 'if-replacement-exists',
            replacement: path.resolve(
              __dirname,
              'src/components/RobotAvatar/placeholder_highres.json',
            ),
            async: true,
          },
        },
      ],
    },
    output: {
      path: path.resolve(__dirname, '../android/app/src/main/assets/static/frontend'),
      filename: `main.v${version}.[contenthash].js`,
      clean: true,
      publicPath: 'auto',
    },
    plugins: [
      new HtmlWebpackPlugin({
        publicPath: './static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: false,
          mobile: true,
        },
        filename: path.resolve(__dirname, '../android/app/src/main/assets/index.html'),
        inject: 'body',
        robosatsSettings: 'mobile-basic',
        basePath: 'file:///android_asset/',
      }),
      new CopyWebpackPlugin({
        patterns: [
          {
            from: path.resolve(__dirname, 'static/css'),
            to: path.resolve(__dirname, '../android/app/src/main/assets/static/css'),
            transform(content, path) {
              if (path.endsWith('.css')) {
                let cssContent = content.toString();
                cssContent = cssContent.replace(
                  /url\(\/static\/css\/fonts\/roboto/g,
                  'url(file:///android_asset/static/css/fonts/roboto',
                );
                return Buffer.from(cssContent);
              }
              return content;
            },
          },
          {
            from: path.resolve(__dirname, 'static/assets/sounds'),
            to: path.resolve(__dirname, '../android/app/src/main/assets/static/assets/sounds'),
          },
          {
            from: path.resolve(__dirname, 'static/federation'),
            to: path.resolve(__dirname, '../android/app/src/main/assets/static/assets/federation'),
          },
        ],
      }),
    ],
  };
};

// Kilowallet's ROBLO tab: the Android client without its native parts. Robot names and avatars
// come from the WebAssembly build (as on the web), the page is served by Kilowallet from a local
// https origin (WebViewAssetLoader's appassets.androidplatform.net) and talks to Kilowallet's
// bridge, which reimplements AndroidAppRobosats on Kilowallet's Tor and storage. Built only with
// `--env target=kilowallet` (scripts/kilowallet-client.sh); the regular builds are unchanged.
export const KILOWALLET_BASE = 'https://appassets.androidplatform.net/roblo/';
const KILOWALLET_OUT = path.resolve(__dirname, 'build-kilowallet/roblo');

const configKilowallet = (env: any, _argv: { mode: string }): Configuration => {
  const replace = (from: string, to: string) => ({
    test: path.resolve(__dirname, from),
    loader: 'file-replace-loader',
    options: {
      condition: 'if-replacement-exists',
      replacement: path.resolve(__dirname, to),
      async: true,
    },
  });
  return {
    ...config,
    // Reproducible: built from a fixed folder (scripts/kilowallet-client.sh), and node_modules'
    // symlink is not resolved, because WebAssembly file names depend on the modules' paths.
    resolve: { ...config.resolve, symlinks: false },
    module: {
      ...config.module,
      rules: [
        ...(config?.module?.rules || []),
        replace('src/i18n/Web.js', 'src/i18n/Mobile.js'),
        replace('src/geo/Web.js', 'src/geo/Mobile.js'),
        replace(
          'src/components/RobotAvatar/placeholder.json',
          'src/components/RobotAvatar/placeholder_highres.json',
        ),
      ],
    },
    output: {
      path: path.resolve(KILOWALLET_OUT, 'static/frontend'),
      filename: `main.v${version}.[contenthash].js`,
      clean: true,
      publicPath: 'auto',
    },
    plugins: [
      new DefinePlugin({ __MOBILE_ASSET_BASE__: JSON.stringify(KILOWALLET_BASE) }),
      new HtmlWebpackPlugin({
        publicPath: './static/frontend/',
        template: path.resolve(__dirname, 'templates/frontend/index.ejs'),
        templateParameters: {
          pro: false,
          mobile: true,
        },
        filename: path.resolve(KILOWALLET_OUT, 'index.html'),
        inject: 'body',
        robosatsSettings: 'mobile-basic',
        basePath: KILOWALLET_BASE,
      }),
      new CopyWebpackPlugin({
        patterns: [
          {
            from: path.resolve(__dirname, 'static/css'),
            to: path.resolve(KILOWALLET_OUT, 'static/css'),
            transform(content, path) {
              if (path.endsWith('.css')) {
                const cssContent = content
                  .toString()
                  .replace(
                    /url\(\/static\/css\/fonts\/roboto/g,
                    `url(${KILOWALLET_BASE}static/css/fonts/roboto`,
                  );
                return Buffer.from(cssContent);
              }
              return content;
            },
          },
          {
            from: path.resolve(__dirname, 'static/assets/sounds'),
            to: path.resolve(KILOWALLET_OUT, 'static/assets/sounds'),
          },
          {
            from: path.resolve(__dirname, 'static/federation'),
            to: path.resolve(KILOWALLET_OUT, 'static/assets/federation'),
          },
        ],
      }),
    ],
  };
};

export default (env: any, argv: { mode: string }) =>
  env?.target === 'kilowallet'
    ? [configKilowallet(env, argv)]
    : [configNode(env, argv), configAndroid(env, argv)];
