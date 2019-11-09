import os
import boto3
from botocore.vendored import requests
import urllib.parse
from io import BytesIO
from gzip import GzipFile
from botocore.exceptions import ClientError
from urllib.parse import unquote_plus
import traceback


# PCE API request call using requests module
def pce_request(pce, org_id, key, secret, verb, path, params=None,
                data=None, json=None, extra_headers=None):
    base_url = os.path.join(pce, 'orgs', org_id)
    headers = {
              'user-agent': 'aws-lambda-quarantine',
            }
    print(base_url)
    if extra_headers:
        headers.update(extra_headers)
        print(headers)
    if json:
        print(json)
    response = requests.request(verb,
                                os.path.join(base_url, path),
                                auth=(key, secret),
                                headers=headers,
                                params=params,
                                json=json,
                                data=data)
    return response


# Fetching the Illumio policies from IP list
def update_illumio_policies_ip_list():
    # Getting the data from environment variables for the PCE API request
    pce_api = int(os.environ['ILO_API_VERSION'])
    pce = os.path.join('https://' + os.environ['ILLUMIO_SERVER'] + ':' + os.environ['ILO_PORT'], 'api', 'v%d' % pce_api)
    org_id = os.environ['ILO_ORG_ID']
    key = 'api_' + os.environ['ILO_API_KEY_ID']
    secret = os.environ['ILO_API_KEY_SECRET']
    ip_list = os.environ['SECURITY_LIST_KEY']
    ip_list_href = 'sec_policy/draft/ip_lists/' + str(ip_list)
    response = pce_request(pce, org_id, key, secret, 'GET', ip_list_href).json()
    print('Received the following IP List from Illumio PCE')
    print(response)
    pce_ip_list = []
    for ip_obj in response['ip_ranges']:
        pce_ip_list.append(ip_obj.get('from_ip', None))
    return pce_ip_list


# Fetching the Illumio security policy for RDS
def update_illumio_policies():
    pce_ip_list = []
    status = {}
    status['ip_list'] = []
    status['db_dict'] = {}
    # Getting the data from environment variables for the PCE API request
    pce_api = int(os.environ['ILO_API_VERSION'])
    pce = os.path.join('https://' + os.environ['ILLUMIO_SERVER'] + ':' + os.environ['ILO_PORT'], 'api', 'v%d' % pce_api)
    org_id = os.environ['ILO_ORG_ID']
    key = 'api_' + os.environ['ILO_API_KEY_ID']
    secret = os.environ['ILO_API_KEY_SECRET']
    sec_policy = 'sec_policy/draft/rule_sets/' + str(os.environ['ILLUMIO_RULESET_KEY']) + '?representation=rule_set_services_labels_and_names'
    response = pce_request(pce, org_id, key, secret, 'GET', sec_policy).json()
    print('Received the following response from Illumio PCE for sec_policy')
    print(response)
    # base_labels = response['scopes'][0]
    role_label = response['rules'][0]['consumers'][0]['label']['href']
    role_label = str('[["') + role_label + str('"]]')
    query = urllib.parse.quote_plus(role_label)
    workload = 'workloads?representation=workload_labels&labels=' + query
    wl_response = pce_request(pce, org_id, key, secret, 'GET', workload).json()
    print('This is the response for consumer workloads from the PCE ', wl_response)
    for workload in wl_response:
        pce_ip_list.append(workload['interfaces'][0]['address'])
        if len(workload.get('interfaces')) >= 2:
            pce_ip_list.append(workload['interfaces'][1]['address'])
    status['ip_list'] = pce_ip_list
    print('This is the ip list for workloads from PCE ', status)
    service_role_label = response['rules'][0]['providers'][0]['label']['href']
    service_role_label = str('[["') + service_role_label + str('"]]')
    vs_query = urllib.parse.quote_plus(service_role_label)
    virtual_service = 'sec_policy/draft/virtual_services?max_results=500&representation=virtual_service_labels_services_and_workloads&labels=' + vs_query
    print(virtual_service)
    vs_response = pce_request(pce, org_id, key, secret, 'GET', virtual_service).json()
    print('Received the following response from Illumio PCE for virtual services')
    print(vs_response)
    if vs_response is not None:
        rds_endpoint = vs_response[0]['service_addresses'][0]['fqdn']
        rds_workload = vs_response[0]['bound_workloads'][0]['href']
        print('The virtual_service FQDN is ', rds_endpoint, ' and the bound workload is ', rds_workload)
        db_instance_identifier = rds_endpoint.split('.')[0]
        status['db_instance_identifier'] = db_instance_identifier
        return status
    else:
        return None


def update_aws_rds_security_group(status_dict):
    region = os.environ['AWS_REGION']
    rds = boto3.client('rds', region_name=region)
    db = rds.describe_db_instances(DBInstanceIdentifier=status_dict['db_instance_identifier'])
    db_dict = {}
    print('The response for AWS describe_db_instances is', db)
    db_dict['fqdn'] = db['DBInstances'][0]['Endpoint']['Address']
    db_dict['security_group_id'] = db['DBInstances'][0]['VpcSecurityGroups'][0]['VpcSecurityGroupId']
    db_dict['vpc_id'] = db['DBInstances'][0]['DBSubnetGroup']['VpcId']
    db_dict['db_parameter_group_name'] = db['DBInstances'][0]['DBParameterGroups'][0]['DBParameterGroupName']
    print("This is the db related info")
    print(db_dict)
    # security_group_id = db_dict['security_group_id']
    port_range_start = 3306
    port_range_end = 3306
    protocol = 'TCP'
    # Creating an ec2 client
    ec2 = boto3.client('ec2')
    group_name = str('Security group-' + db_dict['fqdn'])
    try:
        get_sec_response = ec2.describe_security_groups(Filters=[
                    {'Name': 'vpc-id', 'Values': [db['DBInstances'][0]['DBSubnetGroup']['VpcId']]},
                    {'Name': 'group-name', 'Values': [group_name]}
                                                      ])
        print('Does the security group exist in this VPC?', get_sec_response)
    except ClientError as e:
        print(e)
    if get_sec_response.get('SecurityGroups') != []:
        if get_sec_response.get('SecurityGroups')[0].get('GroupName') == group_name:
            print('Security group managed by Illumio already exists')
            current_sec_group_id = get_sec_response['SecurityGroups'][0]['GroupId']
            print('Security Group Created %s in vpc %s.' % (current_sec_group_id, db_dict['vpc_id']))
    else:
        try:
            response = ec2.create_security_group(GroupName=str('Security group-' + db_dict['fqdn']),
                                                 Description='ILLUMIO Managed security_group for RDS',
                                                 VpcId=db_dict['vpc_id'])
            print(response)
            current_sec_group_id = response['GroupId']
            print('Security Group Created %s in vpc %s.' % (current_sec_group_id, db_dict['vpc_id']))
        except ClientError as e:
            print('This is the error response for security group creation')
            print(e)
    # Get Security Group and based on workload mode - based on the mode delete the SG and create a new SG
    # Associate the SG with the RDS instance
    # Updating the rds with the newly created SG
    resp = rds.modify_db_instance(DBInstanceIdentifier=db_dict['fqdn'].split('.')[0],
                                  VpcSecurityGroupIds=[current_sec_group_id, ],
                                  DBParameterGroupName= db_dict['db_parameter_group_name'],
                                  ApplyImmediately=True,
                                  )
    print(resp)
    # Adding rules to the new security group
    ec2s = boto3.resource('ec2')
    try:
        current_sec_group_response = ec2.describe_security_groups(GroupIds=[current_sec_group_id])
        print('Rules present in security group are as follows:', current_sec_group_response)
        if current_sec_group_response['SecurityGroups'] != []:
            rules_ip_list = []
            for security_group in current_sec_group_response['SecurityGroups']:
                for ips in security_group.get('IpPermissions'):
                    for cidrs in ips['IpRanges']:
                        rules_ip_list.append(cidrs['CidrIp'])
            print(rules_ip_list)
    except ClientError as e:
        print(e)
    security_group = ec2s.SecurityGroup(current_sec_group_id)
    for ip in status_dict['ip_list']:
        cidr = ip + "/32"
        if cidr not in rules_ip_list:
            description = os.environ['SECURITY_GROUP_DESC']
            resp = security_group.authorize_ingress(
                DryRun=False,
                IpPermissions=[
                    {
                        'FromPort': port_range_start,
                        'ToPort': port_range_end,
                        'IpProtocol': protocol,
                        'IpRanges': [
                            {
                                'CidrIp': cidr,
                                'Description': description
                            },
                        ]
                    }
                ]
            )
            print('This ', cidr, 'is added to the security group')
            print(resp)
        else:
            print(cidr, 'is already present in the rules')
    return "success"


# Upload RDS flow logs from S3 to PCE
def get_flow_logs_from_s3(event):
    flows = " "
    headers = {
        'Content-Encoding': 'deflate',
        'X-Bulk-Traffic-Load-CSV-Version': '1',
        'Content-Type': "text/csv"
    }
    pce_api = int(os.environ['ILO_API_VERSION'])
    pce = os.path.join('https://' + os.environ['ILLUMIO_SERVER'] + ':' + os.environ['ILO_PORT'], 'api', 'v%d' % pce_api)
    org_id = os.environ['ILO_ORG_ID']
    api_key = 'api_' + os.environ['ILO_API_KEY_ID']
    secret = os.environ['ILO_API_KEY_SECRET']
    traffic_flows = 'agents/bulk_traffic_flows'
    try:
        s3 = boto3.resource(u's3')
        s3 = boto3.client('s3')
        for record in event['Records']:
            print(record)
        # get a handle on the bucket that holds your file
        bucket_name = record['s3']['bucket']['name']
        # get a handle on the object you want (i.e. your file)
        key = unquote_plus(record['s3']['object']['key'])
        data = s3.get_object(Bucket=bucket_name, Key=key)
        bytestream = BytesIO(data['Body'].read())
        got_text = GzipFile(None, 'rb', fileobj=bytestream).read().decode('utf-8')
        flow_logs = got_text.split('\n')
        for flow in flow_logs:
            flow_data = flow.split(" ")
            if len(flow_data) >= 4:
                if flow_data[4] == '2' or flow_data[4] == '3' or flow_data[4] == '19':
                    # print(flow_data)
                    flows += ("%s,%s,%s,%s\n" % (flow_data[0], flow_data[1], flow_data[2], flow_data[3]))
        # Getting the data from environment variables for the PCE API request
        print('The individual flows sent to the PCE are ')
        print(flows)
        response = pce_request(pce, org_id, api_key, secret, 'POST', traffic_flows, data=flows, extra_headers=headers).json()
        print(response)
        return response
    except Exception as e:
        print(traceback.format_exc())
        print(e)


def lambda_handler(event, context):
    print(event)
    flows = get_flow_logs_from_s3(event)
    print(flows)
    # Removing the enforcement from Lambda
    status_dict = update_illumio_policies()
    # print(status['ip_list'])
    status = None
    status = update_aws_rds_security_group(status_dict)
    print(status)
    return {
        'statusCode': update_aws_rds_security_group(status_dict),
        'body': status_dict
    }
