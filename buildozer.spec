[app]

# ZionBrowser Android
title = ZionBrowser
package.name = zionbrowser
package.domain = io.standardbitcoin

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

version = 2.0.0

requirements = python3,kivy

orientation = portrait
fullscreen = 0

# Android permissions
android.permissions = INTERNET,ACCESS_NETWORK_STATE

# Android API levels
android.api = 34
android.minapi = 28
android.ndk = 25c
android.accept_sdk_license = True

# App icon
icon.filename = icon.png

# Presplash
presplash.filename = presplash.png

# Build
android.archs = arm64-v8a,armeabi-v7a
android.release_artifact = aab

# Don't include unneeded Python modules
android.enable_androidx = True

[buildozer]
log_level = 2
warn_on_root = 1
