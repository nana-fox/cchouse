#!/bin/bash
# run as claude: locale, US units, Pinyin, no screensaver password
set -eu
defaults write -g AppleLanguages -array en-US
defaults write -g AppleLocale en_US
defaults write -g AppleMeasurementUnits Inches
defaults write -g AppleMetricUnits -bool false
defaults write -g AppleTemperatureUnit Fahrenheit
defaults write -g AppleICUForce12HourTime -bool true
defaults write com.apple.screensaver askForPassword -int 0
defaults -currentHost write com.apple.screensaver idleTime -int 0
P=com.apple.HIToolbox
defaults write $P AppleEnabledInputSources -array \
 '<dict><key>InputSourceKind</key><string>Keyboard Layout</string><key>KeyboardLayout ID</key><integer>0</integer><key>KeyboardLayout Name</key><string>U.S.</string></dict>' \
 '<dict><key>Bundle ID</key><string>com.apple.inputmethod.SCIM</string><key>InputSourceKind</key><string>Keyboard Input Method</string></dict>' \
 '<dict><key>Bundle ID</key><string>com.apple.inputmethod.SCIM</string><key>Input Mode</key><string>com.apple.inputmethod.SCIM.ITABC</string><key>InputSourceKind</key><string>Input Mode</string></dict>'
# 跳过首次登录引导（Apple ID、Siri、屏幕使用时间、外观、隐私、Apple Intelligence 等）：
# 必须在 claude 第一次图形登录之前写入；进去直接是桌面，不要让用户在这里登录 Apple ID
SA=com.apple.SetupAssistant
V=$(sw_vers -productVersion); B=$(sw_vers -buildVersion)
for k in DidSeeCloudSetup DidSeeSiriSetup DidSeePrivacy DidSeeScreenTime DidSeeAppearanceSetup \
         DidSeeAccessibility DidSeeTouchIDSetup DidSeeActivationLock DidSeeApplePaySetup \
         DidSeeiCloudLoginForStorageServices DidSeeIntelligence DidSeeTermsOfAddress \
         DidSeeLockdownMode DidSeeAvatarSetup DidSeeSyncSetup DidSeeSyncSetup2 \
         DidSeeTrueTonePrivacy SkipFirstLoginOptimization; do
  defaults write $SA $k -bool true
done
for k in LastSeenCloudProductVersion LastSeenSiriProductVersion LastSeenIntelligenceProductVersion \
         LastSeenDiagnosticsProductVersion LastPreLoginTasksPerformedVersion; do
  defaults write $SA $k "$V"
done
defaults write $SA LastSeenBuddyBuildVersion "$B"
defaults write $SA LastPreLoginTasksPerformedBuild "$B"
defaults write $SA GestureMovieSeen none
# 隐藏桌面小组件：默认的天气组件会提示 "Location Access Needed"，点了就可能打开定位服务
defaults write com.apple.WindowManager StandardHideWidgets -bool true
defaults write com.apple.WindowManager StageManagerHideWidgets -bool true
echo setup-user-done
