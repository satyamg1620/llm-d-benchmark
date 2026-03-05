"""
llmdbenchmark.utilities.kubernetes

Kubernetes Python client helpers for initial cluster connectivity and
platform detection. Used by step 00 to establish a connection before
CommandExecutor takes over for all subsequent kubectl/helm/helmfile calls.
"""

from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException


def kube_connect(
    kubeconfig: str | None = None,
    cluster_url: str | None = None,
    token: str | None = None,
) -> client.ApiClient:
    """Establish a Kubernetes API connection.

    Tries, in order:
    1. Explicit kubeconfig file
    2. Cluster URL + token (bearer token auth)
    3. Default kubeconfig (~/.kube/config or KUBECONFIG env var)
    4. In-cluster config (when running inside a pod)
    """
    if kubeconfig:
        k8s_config.load_kube_config(config_file=kubeconfig)
        return client.ApiClient()

    if cluster_url and token:
        configuration = client.Configuration()
        configuration.host = cluster_url
        configuration.api_key = {"authorization": f"Bearer {token}"}
        configuration.verify_ssl = False
        return client.ApiClient(configuration)

    try:
        k8s_config.load_kube_config()
        return client.ApiClient()
    except k8s_config.ConfigException:
        k8s_config.load_incluster_config()
        return client.ApiClient()


def is_openshift(api_client: client.ApiClient) -> bool:
    """Detect if the cluster is OpenShift by checking for OpenShift API groups."""
    try:
        api = client.ApisApi(api_client)
        groups = api.get_api_versions()
        for group in groups.groups:
            if group.name and "openshift" in group.name.lower():
                return True
    except ApiException:
        pass
    return False


def get_service_endpoint(
    api_client: client.ApiClient,
    namespace: str,
    service_name: str,
) -> str | None:
    """Get the cluster IP and port for a Service, returned as ``ip:port``."""
    v1 = client.CoreV1Api(api_client)
    try:
        service = v1.read_namespaced_service(
            name=service_name, namespace=namespace
        )
        cluster_ip = service.spec.cluster_ip
        if service.spec.ports:
            port = service.spec.ports[0].port
            return f"{cluster_ip}:{port}"
        return cluster_ip
    except ApiException:
        return None


def get_gateway_address(
    api_client: client.ApiClient,
    namespace: str,
    gateway_name: str,
) -> str | None:
    """Get the first address from a Gateway custom resource's status."""
    custom_api = client.CustomObjectsApi(api_client)
    try:
        gateway = custom_api.get_namespaced_custom_object(
            group="gateway.networking.k8s.io",
            version="v1",
            namespace=namespace,
            plural="gateways",
            name=gateway_name,
        )
        addresses = gateway.get("status", {}).get("addresses", [])
        if addresses:
            return addresses[0].get("value")
    except ApiException:
        pass
    return None
