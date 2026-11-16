#!/usr/bin/env groovy

// LedgerKernel CI —— 由 GitHub webhook 触发，在一次性 Python 容器中跑测试。
// 服务端与仓库端配置步骤见 docs/ci-github-webhook.md。

pipeline {
    agent any

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    // 触发分支 / PR / tag 的白名单在 Jenkins job 配置与 GitHub webhook 中限制，
    // 不在 Pipeline 里重复判断，避免分支被推送时没有任何构建反馈。
    environment {
        PYTHONUNBUFFERED = '1'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }

    stages {
        // 默认 checkout 隐式使用 scm，webhook 推送的分支 / PR / tag 都会被正确
        // 检出；构建状态也由 Git 插件按同一 commit 回写，不需要手动 checkout。
        stage('Checkout') {
            steps {
                sh '''
                    git log -1 --oneline
                    git status --short
                '''
            }
        }

        stage('测试与 CLI 冒烟') {
            steps {
                sh '''
                    set -eu
                    # Jenkins 容器内没有 Python；把工作区通过 tar 流送进
                    # 一次性 Python 容器，避免依赖宿主路径或全局安装。
                    tar --exclude=.git --exclude=.venv --exclude='__pycache__' -cf - . \
                      | docker run --rm -i python:3.12-slim bash -lc '
                          set -eux
                          mkdir -p /workspace
                          tar -xf - -C /workspace
                          cd /workspace
                          python -m pip install --upgrade pip
                          python -m pip install -e .
                          python -m unittest discover -s tests -t . -v
                          ledger-kernel --version
                          ledger-kernel --help > /dev/null
                        '
                '''
            }
        }
    }

    post {
        success {
            echo '✅ LedgerKernel CI 通过'
        }
        failure {
            echo '❌ LedgerKernel CI 失败，请查看测试日志'
        }
        always {
            // GitHub commit check 状态由 Jenkins Git 插件自动回写（"LedgerKernel CI"）：
            // 构建结束时写 SUCCESS / FAILURE；不要在这里手写状态，以免盖掉真实结果。
            cleanWs()
        }
    }
}
