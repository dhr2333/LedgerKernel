pipeline {
    agent any

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    environment {
        PYTHONUNBUFFERED = '1'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
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
            cleanWs()
        }
    }
}
